import inspect
import subprocess
import sys

import bcrypt
from fastapi import FastAPI, HTTPException
import httpx
import pytest

from core.auth_credentials import load_auth_users, load_signing_secret
from core.config import Application, ConfigDict
from core.orm.auth import RevokedToken
from core.orm.engine import engine
from web.api.auth_routes import router
from web.utils import auth


@pytest.fixture
def auth_config(monkeypatch, tmp_path):
    config_path = tmp_path / "custom.yaml"
    record = {"password_hash": bcrypt.hashpw(b"test-login", bcrypt.gensalt(rounds=4)).decode(), "ak": "test-ak", "sk_hash": "test-sk-hash"}
    config = ConfigDict({"auth": {"users": {"admin": record}}})
    config.save_to_file(str(config_path))
    config._source_path = str(config_path)
    monkeypatch.setattr(ConfigDict, "get_instance", lambda: config)
    RevokedToken.__table__.create(engine, checkfirst=True)
    return config, config_path


@pytest.mark.asyncio
async def test_login_protected_and_logout_need_no_user_body(auth_config):
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/login", json={"username": "admin", "password": "test-login"})
        assert login.status_code == 200
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert (await client.get("/protected/unified", headers=headers)).status_code == 200
        logout = await client.post("/logout", headers=headers)
        assert logout.status_code == 200
        assert logout.json().get("new_access_token") is None
        assert (await client.get("/protected/unified", headers=headers)).status_code == 401
    assert "current_user" not in inspect.signature(router.routes[-1].endpoint).parameters


@pytest.mark.asyncio
async def test_rotation_rejects_existing_session_without_restart(auth_config):
    config, path = auth_config
    token, _ = auth.create_access_token({"sub": "admin"})
    user, _ = await auth.get_current_user(authorization=f"Bearer {token}")
    assert user.username == "admin"
    config.auth.users.admin.password_hash = bcrypt.hashpw(b"new-password", bcrypt.gensalt(rounds=4)).decode()
    config.save_to_file(str(path))
    with pytest.raises(HTTPException) as error:
        await auth.get_current_user(authorization=f"Bearer {token}")
    assert error.value.status_code == 401
    assert load_auth_users()["admin"]["password_hash"] == config.auth.users.admin.password_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [None, "Bearer invalid"])
async def test_legacy_aksk_fails_closed_without_default_secret(auth_config, authorization):
    with pytest.raises(HTTPException) as error:
        await auth.get_current_user(authorization=authorization, ak="test-ak", timestamp="20260928120000", nonce="nonce", signature="signature")
    assert error.value.status_code == 401


def test_revocation_is_visible_in_another_process(auth_config):
    from core.orm.auth import revoke_token
    import time

    token = "fake-process-token"
    revoke_token(token, int(time.time()) + 300)
    result = subprocess.run([sys.executable, "-c", "from core.orm.auth import is_token_revoked; assert is_token_revoked('fake-process-token')"], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_signing_key_is_private_and_independent_of_tls(monkeypatch, tmp_path):
    monkeypatch.setattr(Application, "ROOT_DIR", str(tmp_path))
    secret = load_signing_secret()
    path = tmp_path / "config/jwt-signing.key"
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_signing_secret() == secret
    assert len(secret) >= 43


def test_signing_key_rejects_symlink(monkeypatch, tmp_path):
    monkeypatch.setattr(Application, "ROOT_DIR", str(tmp_path))
    (tmp_path / "config").mkdir()
    outside = tmp_path / "outside"
    outside.write_text("not-a-key")
    (tmp_path / "config/jwt-signing.key").symlink_to(outside)
    with pytest.raises(OSError):
        load_signing_secret()


def test_password_change_writes_back_to_source_path(auth_config, monkeypatch):
    from cli.app import set_password

    config, path = auth_config
    set_password.callback("new-test-password")
    reloaded = ConfigDict.load_from_file(str(path), use_cache=False)
    assert bcrypt.checkpw(b"new-test-password", reloaded.auth.users.admin.password_hash.encode())
    assert path.stat().st_mode & 0o777 == 0o600


def test_failed_configuration_write_keeps_previous_credentials(auth_config, monkeypatch):
    import core.config.config_dict as config_module

    config, path = auth_config
    before = path.read_bytes()

    def interrupted_dump(data, stream, **kwargs):
        stream.write("auth: partial")
        raise OSError("simulated interrupted write")

    monkeypatch.setattr(config_module.yaml, "safe_dump", interrupted_dump)
    with pytest.raises(OSError):
        config.save_to_file(str(path))
    assert path.read_bytes() == before
    assert not list(path.parent.glob(f".{path.name}.*"))
