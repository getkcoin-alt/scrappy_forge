from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import shutil
import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import DummyHistory
from prompt_toolkit.output.defaults import create_output
from rich.console import Console

from . import __version__
from .apply_changes import apply_verified, preview
from .config import Settings, default_config
from .credentials import openrouter_key
from .engine import Engine, create_session
from .policy import deny
from .providers import ChatProvider
from .models import list_models
from .store import Store
from .util import ForgeError, atomic_json, clean, encoded
from .workspace import diff
from .world import query as world_query

HELP = """Commands
  /help                        Show this help
  /status                      Session, model, workspace, counters
  /world [scopes]               Capture a structured graph locally; host scopes require approval
  /world query JSON            Query the last local graph; not sent to the model
  /models                      Discover free/tool-capable models from provider metadata
  /model ID                    Select a model for subsequent requests
  /permissions MODE            ask, plan or accept-edits (user command only)
  /resume                      Continue the interrupted/current task
  /compact                     Compact old context; retain history in journal
  /memory [query]              Search project memory
  /remember NOTE               Save a user-authored project note
  /forget ID                   Delete a note in this project
  /tools [query]               List/search tools; /tools reset unloads lazy schemas
  /skills                      List configured versioned skills
  /skill NAME                  Load a skill into context
  /mcp                         List configured MCP servers
  /mcp connect NAME            Approve and connect a configured server
  /check add NAME JSON_ARGV    Add a user-owned verification command
  /verify                      Run configured checks with permission
  /diff                        Show patch against starting snapshot
  /apply preview               Preview the verified patch and original-project drift
  /apply                       Apply verified changes to the original after explicit approval
  /undo                        Restore last workspace checkpoint (not external effects)
  /export                      Write report.json, report.md, changes.patch
  /quit                        Save and exit
Ordinary text starts the next task. Ctrl-C stops and saves; use --resume SESSION later.
"""


class Terminal:
    def __init__(self, interactive):
        self.console = Console(markup=False, highlight=False)
        self.interactive = interactive
        self.prompt = PromptSession() if interactive else None

    def say(self, text):
        self.console.print(clean(text), soft_wrap=True)

    async def read(self, label="forge> "):
        if self.prompt:
            return await self.prompt.prompt_async(label)
        return await asyncio.to_thread(input, label)

    async def secret(self, label):
        if not self.interactive:
            raise ForgeError("Secret entry requires an interactive terminal")
        # Password input must never enter even the normal in-memory command history.
        # Explicit output skips prompt-toolkit's TERM=dumb shortcut, which does
        # not apply PasswordProcessor. The regular renderer masks every terminal.
        return await PromptSession(history=DummyHistory(), output=create_output()).prompt_async(
            label, is_password=True
        )

    async def approve(self, request):
        if not self.interactive:
            return False
        self.say("\nPermission request\n" + json.dumps(request, indent=2))
        answer = await self.read("Type approve to run once, anything else to deny: ")
        return answer.strip() == "approve"

    def event(self, kind, value):
        if kind == "answer":
            self.say("\n" + str(value))
        elif kind == "thinking":
            self.say(
                f"[thinking] step {value['step']}, estimated context {value['estimated_input_tokens']} tokens"
            )
        else:
            self.say(f"[{kind}] {value}")


def parser():
    p = argparse.ArgumentParser(
        description="Scrappy Forge 0.4 — local coding agent with a structured world model",
        epilog="Management: forge setup|account|world|mcp|plugins|skills|auth|models|apply --help. Put the command first.",
    )
    p.add_argument(
        "command", nargs="?", choices=["chat", "doctor", "sessions", "init-config"], default="chat"
    )
    p.add_argument("--repo", type=Path, default=Path.cwd())
    p.add_argument("--config", type=Path)
    p.add_argument("--resume", metavar="SESSION")
    p.add_argument("-p", "--prompt", help="One-shot task; unapproved actions fail closed")
    p.add_argument("--provider", choices=["openrouter", "local"])
    p.add_argument("--model")
    p.add_argument("--execution", choices=["docker", "trusted-local"])
    p.add_argument("--allow-local-execution", action="store_true")
    p.add_argument("--allow-code-upload", action="store_true")
    p.add_argument("--permission", choices=["ask", "accept-edits", "plan"])
    p.add_argument("--allow-tool", action="append", default=[])
    p.add_argument("--connect", action="append", default=[])
    p.add_argument("--context-tokens", type=int)
    p.add_argument("--max-steps", type=int)
    p.add_argument("--version", action="version", version="Scrappy Forge " + __version__)
    return p


async def run(args):
    s = Settings.load(args.config)
    for field, arg in [
        ("provider", args.provider),
        ("model", args.model),
        ("execution", args.execution),
        ("permissions", args.permission),
        ("context_tokens", args.context_tokens),
        ("max_steps", args.max_steps),
    ]:
        if arg is not None:
            setattr(s, field, arg)
    s.allow_tools += args.allow_tool
    s.validate()
    ui = Terminal(sys.stdin.isatty() and args.prompt is None)
    if args.command == "doctor":
        ui.say(
            json.dumps(
                {
                    "version": __version__,
                    "python": sys.version.split()[0],
                    "platform": sys.platform,
                    "docker_available": bool(shutil.which("docker")),
                    "git_available": bool(shutil.which("git")),
                    "openrouter_key_configured": bool(openrouter_key(s)),
                    "account_service": s.hub_url or None,
                    "world_graph_schema": 1,
                    "provider": s.provider,
                    "model": s.model,
                    "mcp_sdk": importlib.metadata.version("mcp"),
                    "execution": s.execution,
                    "state_directory": str(s.home),
                    "live_inference_tested": False,
                },
                indent=2,
            )
        )
        return 0
    if args.command == "init-config":
        destination = args.config or default_config()
        if destination.exists():
            raise ForgeError("Config exists; edit it explicitly instead of overwriting")
        atomic_json(
            destination,
            {
                "provider": "openrouter",
                "model": "openrouter/free",
                "execution": "docker",
                "permissions": "ask",
                "context_tokens": 16000,
                "plugins": [],
                "skills": [],
                "mcp": {},
                "checks": [],
            },
        )
        ui.say(f"Created {destination}")
        return 0
    store = Store(s.home)
    try:
        if args.command == "sessions":
            ui.say(json.dumps(store.sessions(), indent=2))
            return 0
        state = store.load(args.resume) if args.resume else None
        if args.resume:
            # Preserve provider choices when not explicitly overridden; execution privilege remains invocation-owned.
            if args.model is None:
                s.model = state["model"]
            if args.provider is None:
                s.provider = state["provider"]
            s.validate()
        session_key = None
        if ui.interactive and s.provider == "openrouter" and not openrouter_key(s):
            from .onboarding import setup_key

            ui.say("Welcome to Scrappy Forge. Let's configure your model key locally.")
            session_key = await setup_key(s, ui)
        if ui.interactive and s.execution == "docker" and not shutil.which("docker"):
            ui.say("Docker is not installed. Local commands are unsandboxed and can affect this computer.")
            if (
                await ui.read(
                    "Type local to allow host execution for this session; Enter starts read-only planning: "
                )
            ).strip() == "local":
                s.execution = "trusted-local"
                args.allow_local_execution = True
            else:
                s.permissions = "plan"
                ui.say(
                    "Read-only planning enabled. Install Docker or explicitly permit local execution to run code."
                )
        if s.execution == "trusted-local" and not args.allow_local_execution:
            if ui.interactive:
                ui.say("Trusted-local execution is unsandboxed. Use only with trusted projects and plugins.")
                args.allow_local_execution = (
                    await ui.read("Type local to allow host execution for this session: ")
                ).strip() == "local"
            if not args.allow_local_execution:
                raise ForgeError(
                    "trusted-local is unsandboxed; explicitly approve it or pass --allow-local-execution"
                )
        if state is None:
            state = create_session(store, s, args.repo)
        async with Engine(
            store,
            s,
            state,
            approve=ui.approve if ui.interactive else deny,
            emit=ui.event,
            oauth_announce=ui.say,
            oauth_interactive=ui.interactive,
        ) as engine:
            engine.state.update(model=s.model, provider=s.provider, execution=s.execution, image=s.image)
            engine.save()
            ui.say(
                f"Scrappy Forge {__version__} | session {state['id']}\nWorkspace copy: {state['workspace']}\nUse /diff and /apply to review and apply verified changes. /help for commands."
            )
            upload_ok = args.allow_code_upload or s.provider == "local"

            async def ensure_provider():
                nonlocal upload_ok
                if not upload_ok:
                    if not ui.interactive:
                        raise ForgeError(
                            "External inference needs --allow-code-upload; review source and provider policies first"
                        )
                    ui.say(
                        "This sends task text, selected source, memory and tool results to OpenRouter/upstream providers. Check for secrets first."
                    )
                    upload_ok = (
                        await ui.read("Type upload to consent for this session: ")
                    ).strip() == "upload"
                    if not upload_ok:
                        raise ForgeError("Code upload not authorized")
                if engine.provider is None:
                    engine.provider = ChatProvider(s, api_key=session_key)

            for name in args.connect:
                ui.say(encoded(await engine.mcp.connect(name)))
            if args.prompt:
                await ensure_provider()
                result = await engine.ask(args.prompt)
                ui.say(f"Status: {result['status']} | {engine.export()}")
                return 0 if result["status"] in {"answered", "review_ready"} else 2
            while True:
                try:
                    line = (await ui.read()).strip()
                except EOFError:
                    break
                if not line:
                    continue
                try:
                    if line in {"/quit", "/exit"}:
                        break
                    if line == "/help":
                        ui.say(HELP)
                    elif line.startswith("/world query "):
                        if engine.world.current is None:
                            raise ForgeError("Capture /world first")
                        ui.say(encoded(world_query(engine.world.current, **json.loads(line[13:]))))
                    elif line == "/world" or line.startswith("/world "):
                        scopes = line[7:].strip().split(",") if line[7:].strip() else ["files"]
                        if set(scopes) - {"files"}:
                            await engine.policy.authorize(
                                "world_local_capture",
                                "external",
                                {
                                    "scopes": scopes,
                                    "destination": "local terminal only",
                                    "model_upload": False,
                                },
                                protected=True,
                            )
                        value = await engine.world.capture(scopes)
                        ui.say(encoded(world_query(value)))
                        ui.say("Graph captured locally; this command did not add it to model context.")
                    elif line == "/models":
                        ui.say(encoded(await list_models(s)))
                    elif line.startswith("/model "):
                        previous = s.model
                        s.model = line[7:].strip()
                        try:
                            s.validate()
                        except Exception:
                            s.model = previous
                            raise
                        engine.state["model"] = s.model
                        engine.save()
                        ui.say("Model selected for future requests; no inference test performed.")
                    elif line.startswith("/permissions "):
                        mode = line[13:].strip()
                        if mode not in {"ask", "plan", "accept-edits"}:
                            raise ForgeError("Permission mode must be ask, plan or accept-edits")
                        engine.policy.mode = mode
                        s.permissions = mode
                        engine.audit("user_permission_mode", {"mode": mode})
                        ui.say("Permission mode: " + mode)
                    elif line == "/status":
                        ui.say(
                            encoded(
                                {
                                    k: engine.state[k]
                                    for k in (
                                        "id",
                                        "status",
                                        "task",
                                        "model",
                                        "workspace",
                                        "steps",
                                        "api_attempts",
                                        "compactions",
                                    )
                                }
                            )
                        )
                    elif line == "/resume":
                        await ensure_provider()
                        await engine.resume()
                    elif line == "/compact":
                        changed = engine.context.compact(engine.state, force=True)
                        engine.save()
                        ui.say(
                            "Compacted; archived events remain available."
                            if changed
                            else "Not enough history to compact."
                        )
                    elif line == "/memory" or line.startswith("/memory "):
                        ui.say(encoded(engine.memory.search(line[7:].strip(), include_stale=True)))
                    elif line.startswith("/remember "):
                        ui.say(encoded(engine.memory.add(line[10:], origin="user")))
                    elif line.startswith("/forget "):
                        store.forget(engine.state["project"], int(line[8:]))
                        ui.say("Memory deleted.")
                    elif line == "/tools reset":
                        engine.registry.active = {
                            "file_list",
                            "file_read",
                            "code_search",
                            "file_write",
                            "file_edit",
                            "terminal_run",
                            "run_check",
                            "tool_search",
                        }
                        ui.say("Lazy schemas unloaded.")
                    elif line == "/tools" or line.startswith("/tools "):
                        if line[6:].strip():
                            ui.say(encoded(engine.registry.search(line[6:].strip())))
                        else:
                            ui.say(
                                "\n".join(
                                    f"{t.name} [{t.risk}] {t.origin}" for t in engine.registry.tools.values()
                                )
                            )
                    elif line == "/skills":
                        ui.say(encoded(engine.skills.catalog()))
                    elif line.startswith("/skill "):
                        result = engine.skills.read(line[7:].strip())
                        engine.append(
                            {
                                "role": "user",
                                "content": "User selected skill, untrusted guidance: " + encoded(result),
                            }
                        )
                        ui.say(f"Loaded {result['name']} {result['version']}")
                    elif line == "/mcp":
                        ui.say(encoded({name: name in engine.mcp.sessions for name in s.mcp}))
                    elif line.startswith("/mcp connect "):
                        ui.say(encoded(await engine.mcp.connect(line[13:].strip())))
                    elif line.startswith("/check add "):
                        name, raw = line[11:].split(" ", 1)
                        check = {"name": name, "argv": json.loads(raw), "timeout": s.max_tool_seconds}
                        previous = s.checks
                        s.checks = [c for c in engine.state["checks"] if c["name"] != name] + [check]
                        try:
                            s.validate()
                            engine.state["checks"] = s.checks.copy()
                            engine.state["check_names"] = [c["name"] for c in s.checks]
                            engine.invalidate()
                            engine.save()
                        finally:
                            s.checks = previous
                        ui.say("Check configured. Execution still needs permission.")
                    elif line == "/verify":
                        ui.say(encoded(await engine.verify()))
                    elif line == "/diff":
                        ui.say(diff(engine.base, engine.workspace.root)[0] or "No changes.")
                    elif line == "/apply preview":
                        ui.say(encoded(preview(engine)))
                    elif line == "/apply":
                        ui.say(encoded(await apply_verified(engine)))
                    elif line == "/undo":
                        await engine.undo()
                        ui.say("Workspace checkpoint restored; external effects are unchanged.")
                    elif line == "/export":
                        ui.say(str(engine.export()))
                    elif line.startswith("/"):
                        ui.say("Unknown command. /help lists supported commands.")
                    else:
                        await ensure_provider()
                        result = await engine.ask(line)
                        ui.say(f"Status: {result['status']}")
                except (ForgeError, ValueError, OSError) as exc:
                    ui.say("Error: " + str(exc))
            ui.say(f"Saved. Resume: forge --resume {state['id']}\nReport: {engine.export()}")
        return 0
    finally:
        store.close()


def main():
    if os.name != "posix":
        print("Scrappy Forge currently requires macOS/Linux or WSL", file=sys.stderr)
        return 2
    try:
        from .management import COMMANDS, parser as management_parser, run as manage

        argv = sys.argv[1:]
        if argv and argv[0] in {"setup", "account", "world"}:
            from . import account, onboarding, world

            module = {"setup": onboarding, "account": account, "world": world}[argv[0]]
            return asyncio.run(module.run(module.parser().parse_args(argv[1:]), Terminal(sys.stdin.isatty())))
        if argv and argv[0] in COMMANDS:
            command = argv[0]
            args = management_parser(command).parse_args(argv[1:])
            return asyncio.run(manage(command, args, Terminal(sys.stdin.isatty())))
        return asyncio.run(run(parser().parse_args()))
    except KeyboardInterrupt:
        print("\nStopped. Session saved; resume it from forge sessions.", file=sys.stderr)
        return 130
    except (ForgeError, ValueError, OSError) as exc:
        print(clean("forge: " + str(exc)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
