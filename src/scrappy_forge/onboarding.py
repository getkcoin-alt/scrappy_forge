"""Explicit user-facing setup. No secrets in argv, configuration, or model messages."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import httpx

from .config import Settings
from .credentials import PrivateStore, openrouter_key, validate_key_shape
from .util import ForgeError


async def check_openrouter_key(key, *, transport=None):
    async with httpx.AsyncClient(
        transport=transport, trust_env=False, timeout=15, follow_redirects=False
    ) as c:
        try:
            response = await c.get(
                "https://openrouter.ai/api/v1/key", headers={"Authorization": "Bearer " + key}
            )
        except httpx.HTTPError as exc:
            raise ForgeError(
                "Could not validate the key with OpenRouter. No key was saved; try again later."
            ) from exc
    if response.status_code != 200:
        raise ForgeError(f"OpenRouter key validation returned HTTP {response.status_code}. No key was saved.")
    # Never render the response body: key labels and provider errors may contain private data.
    return True


async def setup_key(settings, ui, *, verify=True):
    if not ui.interactive:
        raise ForgeError("Run forge setup in an interactive terminal, or set OPENROUTER_API_KEY locally")
    ui.say(
        "OpenRouter key: https://openrouter.ai/settings/keys\nInput is hidden. Forge never sends this key to its account server."
    )
    key = validate_key_shape((await ui.secret("OpenRouter API key: ")).strip())
    if verify:
        await check_openrouter_key(key)
        ui.say("Key accepted by OpenRouter. No inference or source-code upload was performed.")
    else:
        ui.say("Key validation skipped. Live inference is not verified.")
    answer = await ui.read(
        "Type save to store this key unencrypted in an owner-only local file; Enter keeps it in memory: "
    )
    if answer.strip() == "save":
        PrivateStore(settings.home).set("openrouter", key)
        ui.say("Saved locally with 0600 file permissions. Remove it with forge setup --forget-key.")
    else:
        ui.say("Key kept only for this process; it will not persist after exit.")
    return key


def parser():
    p = argparse.ArgumentParser(prog="forge setup", description="Configure the local model API key")
    p.add_argument("--config", type=Path)
    p.add_argument("--skip-validation", action="store_true", help="Store without contacting OpenRouter")
    p.add_argument("--forget-key", action="store_true", help="Remove only the locally saved OpenRouter key")
    return p


async def run(args, ui):
    settings = Settings.load(args.config)
    if args.forget_key:
        await asyncio.to_thread(PrivateStore(settings.home).set, "openrouter", None)
        ui.say("Saved OpenRouter key removed. Environment variables and provider grants are unchanged.")
        return 0
    if openrouter_key(settings):
        ui.say(
            "A key is already available. Setup replaces only the saved key; an environment key takes precedence."
        )
    await setup_key(settings, ui, verify=not args.skip_validation)
    ui.say("Next: cd into a trusted project and run forge.")
    return 0
