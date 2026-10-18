"""Read-only prerequisite diagnostics; never install, pull or execute user code."""

from __future__ import annotations

import shutil
import subprocess
import sys


def prerequisite_checks(settings, *, allow_local_execution=False):
    checks = []

    def add(name, required, status, message):
        checks.append({"name": name, "required": required, "status": status, "message": message})

    python_ok = sys.version_info >= (3, 11)
    add("python", True, "ok" if python_ok else "missing",
        f"Running Python {sys.version.split()[0]}; Python 3.11+ is required to run Forge.")
    git = bool(shutil.which("git"))
    add("git", False, "ok" if git else "missing",
        "Checked Git on PATH. Git is optional for Forge's file snapshots and patch export; "
        + ("it is available for Git workflows." if git else "install it separately if your workflow needs it."))
    docker = shutil.which("docker")
    required = settings.execution == "docker"
    add("docker_cli", required, "ok" if docker else "missing",
        "Checked Docker on PATH. " + (
            "Docker is required for code execution in the configured docker mode; planning needs no container."
            if required else "Docker is optional and will not be contacted in trusted-local mode."
        ) + (" Install Docker separately if you intend to use docker execution." if required and not docker else ""))

    def probe(arguments):
        try:
            result = subprocess.run(
                [docker, *arguments], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5, check=False,
            )
        except subprocess.TimeoutExpired:
            return "error", "The metadata check timed out after 5 seconds."
        except OSError:
            return "error", "The Docker executable could not be started."
        return ("ok", "Metadata check succeeded.") if result.returncode == 0 else (
            "error", "The metadata command returned a nonzero exit status."
        )

    if required and docker:
        status, detail = probe(["info", "--format", "{{.ServerVersion}}"])
        add("docker_daemon", True, status,
            "Checked docker info against the selected Docker context. " + detail
            + (" Check that Docker is running, the context is correct and your user can access it."
               if status != "ok" else " No container was started."))
        if status == "ok":
            status, detail = probe(["image", "inspect", "--format", "{{.Id}}", "--", settings.image])
            add("docker_image", True, status,
                "Checked the configured execution image with docker image inspect. " + detail
                + (" Make the configured image available separately; execution uses --pull=never."
                   if status != "ok" else " No image was pulled."))
        else:
            add("docker_image", True, "not_checked", "Image check skipped because Docker access failed.")
    else:
        reason = "Docker CLI is missing." if required else "Docker is not used by trusted-local execution."
        add("docker_daemon", required, "not_checked", reason)
        add("docker_image", required, "not_checked", reason)

    if not required:
        add("local_execution_consent", True, "ok" if allow_local_execution else "blocked",
            "Checked --allow-local-execution for this invocation. Trusted-local runs without a sandbox. "
            + ("The flag is set; normal tool permissions still apply." if allow_local_execution else
               "A noninteractive chat requires this explicit flag; interactive chat asks for consent."))
    add("execution_scope", False, "not_checked",
        f"Configured execution mode: {settings.execution}; permission mode: {settings.permissions}. "
        "Doctor does not run project commands or verify a container workload, provider or MCP server.")
    return checks
