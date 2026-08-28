import fcntl
import os
import stat
import sys
from pathlib import Path

import httpx
import pytest

from scrappy_forge.config import Settings
from scrappy_forge.credentials import PrivateStore, openrouter_key
from scrappy_forge.onboarding import check_openrouter_key, setup_key
from scrappy_forge.util import ForgeError


class UI:
    interactive = True

    def __init__(self, key="fixture-private-key-123456", answer="save"):
        self.key, self.answer, self.messages = key, answer, []
        self.secret_prompts = 0

    def say(self, value):
        self.messages.append(str(value))

    async def read(self, prompt):
        self.messages.append(prompt)
        return self.answer

    async def secret(self, prompt):
        self.secret_prompts += 1
        return self.key


def test_private_credentials_are_owner_only_and_removed_individually(tmp_path):
    vault = PrivateStore(tmp_path)
    vault.set("openrouter", "fixture-openrouter-key")
    vault.set("account", {"token": "fixture-account-token"})
    assert vault.get("openrouter") == "fixture-openrouter-key"
    assert stat.S_IMODE(vault.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(vault.root.stat().st_mode) == 0o700
    vault.set("openrouter", None)
    assert vault.get("openrouter") is None
    assert vault.get("account")["token"] == "fixture-account-token"


def test_environment_key_precedes_saved_key(tmp_path, monkeypatch):
    s = Settings(home=tmp_path)
    PrivateStore(tmp_path).set("openrouter", "fixture-saved-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-env-key")
    assert openrouter_key(s) == "fixture-env-key"


@pytest.mark.parametrize(
    "failure", ["file_symlink", "directory_symlink", "public_file", "public_directory", "hardlink"]
)
def test_unsafe_credential_storage_is_refused(tmp_path, failure):
    vault = PrivateStore(tmp_path / "state")
    vault.set("openrouter", "fixture-key-private")
    if failure == "file_symlink":
        other = tmp_path / "other"
        vault.path.rename(other)
        vault.path.symlink_to(other)
    elif failure == "directory_symlink":
        other = tmp_path / "other-directory"
        vault.root.rename(other)
        vault.root.symlink_to(other, target_is_directory=True)
    elif failure == "public_file":
        vault.path.chmod(0o644)
    elif failure == "public_directory":
        vault.root.chmod(0o755)
    else:
        (tmp_path / "linked").hardlink_to(vault.path)
    with pytest.raises(ForgeError):
        vault.get("openrouter")


def test_credential_update_lock_fails_closed(tmp_path):
    vault = PrivateStore(tmp_path)
    vault.set("openrouter", "fixture-key-private")
    with (vault.root / "write.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ForgeError, match="Another Forge"):
            vault.set("openrouter", "other-fixture-key")


@pytest.mark.asyncio
async def test_setup_secret_does_not_appear_in_messages(tmp_path):
    ui = UI()
    result = await setup_key(Settings(home=tmp_path), ui, verify=False)
    assert result == ui.key and ui.secret_prompts == 1
    assert ui.key not in "\n".join(ui.messages)
    assert PrivateStore(tmp_path).get("openrouter") == ui.key
    assert not list(tmp_path.glob("sessions/*"))


@pytest.mark.asyncio
async def test_setup_memory_only_does_not_write_key(tmp_path):
    ui = UI(answer="")
    await setup_key(Settings(home=tmp_path), ui, verify=False)
    assert PrivateStore(tmp_path).get("openrouter") is None


@pytest.mark.asyncio
async def test_key_validation_sends_only_key_to_fixed_endpoint():
    def handler(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/key"
        assert request.headers["Authorization"] == "Bearer fixture-api-key"
        assert not request.content
        return httpx.Response(200, json={"data": {"label": "private label"}})

    assert await check_openrouter_key("fixture-api-key", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 429, 500])
async def test_key_validation_never_exposes_error_body(status):
    def handler(request):
        return httpx.Response(status, text="fixture-secret-must-not-leak")

    with pytest.raises(ForgeError) as error:
        await check_openrouter_key("fixture-api-key", transport=httpx.MockTransport(handler))
    assert "fixture-secret" not in str(error.value)


def test_corrupt_credentials_error_is_sanitized(tmp_path):
    vault = PrivateStore(tmp_path)
    vault.set("openrouter", "fixture-api-key")
    vault.path.write_text('{"fixture-private-key": invalid}')
    with pytest.raises(ForgeError) as error:
        vault.get("openrouter")
    assert "fixture-private-key" not in str(error.value)


def test_minimal_terminal_masks_key_onboarding(tmp_path):
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "validation/release_smoke.py"
    spec = importlib.util.spec_from_file_location("release_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
    env.update(SCRAPPY_FORGE_HOME=str(tmp_path / "state"), SCRAPPY_FORGE_CONFIG=str(tmp_path / "config.json"))
    module.masked_setup(Path(sys.executable).with_name("forge"), tmp_path, env)
