from __future__ import annotations

import asyncio
import os
import shutil
import signal
import tempfile
import time
import uuid
from pathlib import Path

from .util import ForgeError, bound_env
from .workspace import snapshot, tree_hash


async def process(argv, cwd: Path, *, timeout=60, input_text=None, env=None, max_output=100000):
    if not argv or not all(isinstance(x, str) and "\0" not in x for x in argv):
        raise ForgeError("Command requires a valid argv array")
    base_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        **(env or {}),
    }
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=base_env,
        stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    chunks, size, limited = [], 0, False

    def kill():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def collect():
        nonlocal size, limited
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            available = max(0, max_output - size)
            chunks.append(chunk[:available])
            size += len(chunk)
            if size > max_output:
                limited = True
                kill()
                # Drain the pipe after killing. Leaving a paused StreamReader can
                # prevent asyncio's subprocess transport from completing wait().

    async def feed():
        if input_text is not None:
            try:
                proc.stdin.write(input_text.encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

    timed_out = False
    tasks = [asyncio.create_task(collect()), asyncio.create_task(feed())]
    try:
        async with asyncio.timeout(timeout):
            await proc.wait()
            kill()
            await asyncio.gather(*tasks)
    except TimeoutError:
        timed_out = True
    finally:
        kill()
        await proc.wait()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return {
        "argv": argv,
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "output_limited": limited,
        "output": b"".join(chunks).decode(errors="replace"),
        "seconds": round(time.monotonic() - started, 3),
    }


class Runner:
    def __init__(self, mode="docker", image="python:3.12-slim"):
        self.mode, self.image = mode, image

    async def run(self, argv, work: Path, *, timeout=60, input_text=None, env_names=(), host=False):
        env = bound_env(list(env_names))
        if self.mode == "trusted-local" or host:
            return await process(argv, work, timeout=timeout, input_text=input_text, env=env)
        if not shutil.which("docker"):
            raise ForgeError(
                "Docker unavailable. No host fallback. For trusted code only, opt into trusted-local."
            )
        name = "forge-" + uuid.uuid4().hex
        if "," in str(work):
            raise ForgeError("Docker mount paths containing commas are unsupported")
        command = [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=128",
            "--memory=1g",
            "--cpus=2",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            "/tmp:rw,nosuid,size=128m",
            "--mount",
            f"type=bind,src={work},dst=/work",
            "-w",
            "/work",
            "-e",
            "HOME=/tmp",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
        ]
        if input_text is not None:
            command.append("-i")
        for key in env:
            command += ["-e", key]
        try:
            result = await process(
                [*command, self.image, *argv], work, timeout=timeout, input_text=input_text, env=env
            )
            result["argv"] = argv
            return result
        finally:
            try:
                await process(["docker", "rm", "-f", name], work, timeout=10)
            except (OSError, ForgeError):
                pass

    async def verify(self, source: Path, check: dict):
        before = tree_hash(source)
        with tempfile.TemporaryDirectory(prefix="forge-verify-") as temp:
            work = Path(temp) / "workspace"
            snapshot(source, work)
            result = await self.run(check["argv"], work, timeout=check.get("timeout", 60))
            try:
                mutated = tree_hash(work) != before
            except (OSError, ForgeError):
                mutated = True
        result.update(name=check["name"], tree_hash=before, mutated=mutated, mode=self.mode)
        result["passed"] = (
            result["exit_code"] == 0
            and not result["timed_out"]
            and not result["output_limited"]
            and not mutated
        )
        return result
