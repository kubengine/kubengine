"""Credential rotation must not upgrade an already-authenticated old session."""

from datetime import timedelta

import bcrypt
from fastapi import HTTPException, Request
import pytest

from core.config import ConfigDict
from core.orm.auth import RevokedToken
from core.orm.engine import engine
from web.api import auth_routes
from web.utils import auth


@pytest.fixture
def rotation_config(monkeypatch, tmp_path):
    path = tmp_path / "rotation.yaml"
    config = ConfigDict({"auth": {"users": {"admin": {
        "password_hash": bcrypt.hashpw(b"old-password", bcrypt.gensalt(rounds=4)).decode(),
        "ak": "test-ak", "sk_hash": "test-sk-hash",
    }}}})
    config.save_to_file(str(path))
    config._source_path = str(path)
    monkeypatch.setattr(ConfigDict, "get_instance", lambda: config)
    RevokedToken.__table__.create(engine, checkfirst=True)

    def rotate():
        config.auth.users.admin.password_hash = bcrypt.hashpw(
            b"new-password", bcrypt.gensalt(rounds=4),
        ).decode()
        config.save_to_file(str(path))

    return rotate


@pytest.mark.asyncio
async def test_password_rotated_during_login_cannot_issue_current_session(monkeypatch, rotation_config):
    async def finish_verification_after_rotation(function, *args, **kwargs):
        authenticated = function(*args, **kwargs)
        assert authenticated
        rotation_config()
        return authenticated

    monkeypatch.setattr(auth_routes, "run_in_threadpool", finish_verification_after_rotation)
    with pytest.raises(HTTPException) as error:
        response = await auth_routes.login(auth.LoginRequest(username="admin", password="old-password"))
        await auth.get_current_user(authorization=f"Bearer {response.access_token}")
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_rotation_between_authentication_and_renewal_does_not_upgrade_session(monkeypatch, rotation_config):
    token, _ = auth.create_access_token({"sub": "admin"}, expires_delta=timedelta(minutes=1))
    authenticate = auth.get_current_user

    async def authenticate_then_rotate(**kwargs):
        identity = await authenticate(**kwargs)
        rotation_config()
        return identity

    monkeypatch.setattr(auth, "get_current_user", authenticate_then_rotate)

    @auth.auth_with_renew()
    async def protected(request: Request):
        return {"ok": True}

    request = Request({"type": "http", "headers": [
        (b"authorization", f"Bearer {token}".encode()),
    ]})
    with pytest.raises(HTTPException) as error:
        response = await protected(request)
        renewed = response.new_access_token
        await authenticate(authorization=f"Bearer {renewed}")
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_expired_and_unsigned_tokens_are_rejected(rotation_config):
    import base64
    import json

    expired, _ = auth.create_access_token({"sub": "admin"}, expires_delta=timedelta(minutes=-1))
    valid, _ = auth.create_access_token({"sub": "admin"})
    payload = auth.jwt_instance.decode(valid, auth.signing_key, algorithms={auth.ALGORITHM})
    encode_segment = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    unsigned = encode_segment({"alg": "none", "typ": "JWT"}) + "." + encode_segment(payload) + "."
    prefix, signature = valid.rsplit(".", 1)
    tampered = prefix + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
    for token in (expired, unsigned, tampered):
        with pytest.raises(HTTPException) as error:
            await auth.get_current_user(authorization=f"Bearer {token}")
        assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_nearly_expired_logout_does_not_return_renewed_token(rotation_config):
    token, _ = auth.create_access_token({"sub": "admin"}, expires_delta=timedelta(minutes=1))
    request = Request({"type": "http", "headers": [
        (b"authorization", f"Bearer {token}".encode()),
    ]})
    response = await auth_routes.logout(request)
    assert response.new_access_token is None
    with pytest.raises(HTTPException) as error:
        await auth.get_current_user(authorization=f"Bearer {token}")
    assert error.value.status_code == 401


def test_concurrent_signing_key_initialization_publishes_one_private_key(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from core.auth_credentials import load_signing_secret
    from core.config import Application

    monkeypatch.setattr(Application, "ROOT_DIR", str(tmp_path))
    with ThreadPoolExecutor(max_workers=8) as executor:
        secrets = list(executor.map(lambda _: load_signing_secret(), range(32)))
    assert len(set(secrets)) == 1
    assert (tmp_path / "config/jwt-signing.key").stat().st_mode & 0o777 == 0o600
    assert not list((tmp_path / "config").glob(".jwt-*"))
