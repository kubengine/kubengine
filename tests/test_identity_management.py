"""真实持久化身份、会话撤销、并发管理员保护与过渡期业务封锁。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import bcrypt
import httpx
import pytest
from sqlalchemy import text

from core.auth_credentials import load_auth_user
from core.config import ConfigDict
from core.orm import identity
from web import main
from web.utils import auth

PASSWORD = "initial-password-123"


@pytest.fixture
def store(identity_database, monkeypatch, tmp_path):
    config = ConfigDict(
        {
            "auth": {
                "users": {
                    "admin": {
                        "password_hash": bcrypt.hashpw(
                            PASSWORD.encode(), bcrypt.gensalt(rounds=4)
                        ).decode(),
                        "ak": "legacy-ak",
                        "sk_hash": "legacy-sk-hash",
                    }
                }
            }
        }
    )
    path = tmp_path / "application.yaml"
    config.save_to_file(str(path))
    config._source_path = str(path)
    monkeypatch.setattr(ConfigDict, "get_instance", lambda: config)
    identity.ensure_identity_schema()
    return identity_database, config, path


def add_user(role="readonly", username="reader", grants=()):
    admin = load_auth_user("admin")
    return identity.create_user(
        admin["user_id"],
        username,
        bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode(),
        role,
        ["team-a"],
        list(grants),
    )


def headers(username="admin"):
    token, _ = auth.create_access_token({"sub": username})
    return {"Authorization": f"Bearer {token}"}


def update(actor_id, user, **changes):
    values = {**user, **changes}
    return identity.update_user(
        actor_id,
        user["user_id"],
        values["revision"],
        values["enabled"],
        values["role"],
        values["namespaces"],
        values["sensitive_permissions"],
    )


def client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    )


def test_migration_is_idempotent_and_never_reloads_config_users(store):
    engine, config, path = store
    original = load_auth_user("admin")
    user = add_user()
    update(original["user_id"], user, enabled=False)
    config.auth.users.reader = dict(config.auth.users.admin)
    config.auth.users.admin.password_hash = "changed-config-hash"
    config.save_to_file(str(path))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: identity.ensure_identity_schema(), range(8)))
    assert load_auth_user("admin") == original
    assert load_auth_user("reader")["enabled"] is False
    path.unlink()
    identity.ensure_identity_schema()
    assert load_auth_user("admin") == original
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM console_user")
            ).scalar()
            == 2
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM identity_migration")
            ).scalar()
            == 1
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM identity_role")
            ).scalar()
            == 3
        )


def test_failed_migration_is_atomic_and_can_retry(store, monkeypatch):
    engine, _, _ = store
    # 使用空库模拟首次迁移中途失败，不能留下部分配置用户。
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM console_user"))
        connection.execute(text("DELETE FROM identity_migration"))
    import core.auth_credentials as credentials

    loader = credentials.load_config_auth_users
    monkeypatch.setattr(
        credentials,
        "load_config_auth_users",
        lambda: {
            "one": {"password_hash": "hash"},
            "two": {
                "password_hash": "hash",
                "ak": {"invalid": "sqlite value"},
            },
        },
    )
    with pytest.raises(Exception):
        identity.ensure_identity_schema()
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM console_user")
            ).scalar()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM identity_migration")
            ).scalar()
            == 0
        )
    monkeypatch.setattr(credentials, "load_config_auth_users", loader)
    identity.ensure_identity_schema()
    assert load_auth_user("admin")["enabled"]


@pytest.mark.asyncio
async def test_user_management_and_public_responses(store):
    async with client() as http:
        admin_headers = headers()
        created = await http.post(
            "/api/v1/users",
            headers=admin_headers,
            json={
                "username": "operator",
                "password": PASSWORD,
                "role": "namespace_operator",
                "namespaces": ["team-a", "team-a"],
                "sensitive_permissions": ["pod.files.read"],
            },
        )
        assert created.status_code == 200
        user = created.json()["data"]
        assert user["namespaces"] == ["team-a"]
        assert len(user["user_id"]) == 32
        duplicate = await http.post(
            "/api/v1/users",
            headers=admin_headers,
            json={
                "username": "operator",
                "password": PASSWORD,
            },
        )
        assert duplicate.status_code == 409
        me = await http.get("/api/v1/me", headers=headers("operator"))
        assert me.json()["data"]["role"] == "namespace_operator"
        assert me.json()["data"]["resource_access_enabled"]
        listed = await http.get("/api/v1/users", headers=admin_headers)
        for response in (created, me, listed):
            for secret in (
                "password_hash",
                "sk_hash",
                "auth_revision",
                "legacy-ak",
                PASSWORD,
            ):
                assert secret not in response.text
        body = {
            key: user[key]
            for key in (
                "role",
                "namespaces",
                "sensitive_permissions",
                "revision",
                "enabled",
            )
        }
        body["enabled"] = False
        disabled = await http.put(
            f"/api/v1/users/{user['user_id']}",
            headers=admin_headers,
            json=body,
        )
        assert disabled.status_code == 200
        stale = await http.put(
            f"/api/v1/users/{user['user_id']}",
            headers=admin_headers,
            json=body,
        )
        assert stale.status_code == 409


@pytest.mark.asyncio
async def test_disable_reenable_and_config_cannot_resurrect_sessions(store):
    _, config, path = store
    user = add_user()
    old_headers = headers("reader")
    admin_id = load_auth_user("admin")["user_id"]
    disabled = update(admin_id, user, enabled=False)
    config.auth.users.reader = dict(config.auth.users.admin)
    config.save_to_file(str(path))
    async with client() as http:
        response = await http.post(
            "/api/v1/login", json={"username": "reader", "password": PASSWORD}
        )
        assert response.status_code == 401
        assert (
            await http.get("/api/v1/me", headers=old_headers)
        ).status_code == 401
        update(admin_id, disabled, enabled=True)
        assert (
            await http.get("/api/v1/me", headers=old_headers)
        ).status_code == 401
        assert (
            await http.get("/api/v1/me", headers=headers("reader"))
        ).status_code == 200


@pytest.mark.asyncio
async def test_reset_and_self_password_change_revoke_all_sessions(store):
    user = add_user()
    old1, old2 = headers("reader"), headers("reader")
    async with client() as http:
        reset = await http.post(
            f"/api/v1/users/{user['user_id']}/password",
            headers=headers(),
            json={"password": "reset-password-123"},
        )
        assert reset.status_code == 200
        for old in (old1, old2):
            assert (
                await http.get("/api/v1/me", headers=old)
            ).status_code == 401
        login = await http.post(
            "/api/v1/login",
            json={"username": "reader", "password": "reset-password-123"},
        )
        assert login.status_code == 200
        current = {"Authorization": "Bearer " + login.json()["access_token"]}
        wrong = await http.post(
            "/api/v1/me/password",
            headers=current,
            json={"current_password": "wrong", "password": PASSWORD},
        )
        assert wrong.status_code == 400
        assert (
            await http.get("/api/v1/me", headers=current)
        ).status_code == 200
        changed = await http.post(
            "/api/v1/me/password",
            headers=current,
            json={
                "current_password": "reset-password-123",
                "password": PASSWORD,
            },
        )
        assert changed.status_code == 200
        assert changed.json().get("new_access_token") is None
        assert (
            await http.get("/api/v1/me", headers=current)
        ).status_code == 401


@pytest.mark.asyncio
async def test_demotion_and_grant_revocation_are_visible_with_same_token(
    store,
):
    user = add_user("platform_admin", "second", ["secret.read"])
    token_headers = headers("second")
    async with client() as http:
        assert (
            await http.get("/api/v1/users", headers=token_headers)
        ).status_code == 200
        update(
            load_auth_user("admin")["user_id"],
            user,
            role="readonly",
            namespaces=[],
            sensitive_permissions=[],
        )
        assert (
            await http.get("/api/v1/users", headers=token_headers)
        ).status_code == 403
        me = (await http.get("/api/v1/me", headers=token_headers)).json()[
            "data"
        ]
        assert me["role"] == "readonly"
        assert me["namespaces"] == me["sensitive_permissions"] == []


@pytest.mark.parametrize(
    "enabled,role", [(False, "platform_admin"), (True, "readonly")]
)
def test_cannot_remove_last_admin(store, enabled, role):
    admin = identity.list_users()["items"][0]
    with pytest.raises(identity.IdentityError) as error:
        update(admin["user_id"], admin, enabled=enabled, role=role)
    assert error.value.code == 409
    assert load_auth_user("admin")["enabled"]


@pytest.mark.parametrize("change", [{"enabled": False}, {"role": "readonly"}])
def test_concurrent_last_admin_protection(store, change):
    first = identity.list_users()["items"][0]
    second = add_user("platform_admin", "second")
    barrier = Barrier(2)

    def remove_self(user):
        barrier.wait(timeout=5)
        try:
            update(user["user_id"], user, **change)
            return 200
        except identity.IdentityError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(remove_self, [first, second]))
    assert sorted(results) == [200, 409]
    remaining = [
        item
        for item in identity.list_users()["items"]
        if item["enabled"] and item["role"] == "platform_admin"
    ]
    assert len(remaining) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["readonly", "namespace_operator"])
async def test_legacy_business_paths_are_closed_to_non_admins(store, role):
    add_user(role)
    async with client() as http:
        for path in (
            "/api/v1/users",
            "/api/v1/roles",
            "/api/v1/app/list",
            "/api/v1/k8s/overview",
            "/api/v1/k8s/manifest/secret/team-a/token?reveal_secret=true",
            "/api/v1/k8s/pods/team-a/demo/files?path=/tmp",
            "/api/v1/k8s/pods/team-a/demo/files/download?path=/tmp/token",
            "/api/v1/artifacts/image-import-tasks",
        ):
            response = await http.get(path, headers=headers("reader"))
            assert response.status_code == 403, (path, response.text)
        preview = await http.post(
            "/api/v1/k8s/resources/preview",
            headers=headers("reader"),
            json={
                "yaml_text": (
                    "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test"
                )
            },
        )
        assert preview.status_code == 403
        trigger = await http.post(
            "/api/v1/create-resource",
            headers=headers("reader"),
            json={"resource_name": "demo", "resource_type": "redis"},
        )
        assert trigger.status_code == 403
        assert (
            await http.get("/api/v1/me", headers=headers("reader"))
        ).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"password": "short"},
        {"password": "密" * 25},
        {"namespaces": ["_all"]},
        {"namespaces": ["_cluster"]},
        {"namespaces": ["*"]},
        {"role": "superuser"},
        {"sensitive_permissions": ["*", "users.manage"]},
        {"user_id": "injected"},
    ],
)
async def test_rejects_invalid_identity_input(store, body):
    async with client() as http:
        response = await http.post(
            "/api/v1/users",
            headers=headers(),
            json={"username": "bad", "password": PASSWORD, **body},
        )
    assert response.status_code == 422
    assert load_auth_user("bad") is None
    assert PASSWORD not in response.text


def test_no_user_deletion_route_and_every_business_http_route_has_a_guard():
    def flatten(routes, prefix=""):
        for route in routes:
            if hasattr(route, "original_router"):
                yield from flatten(
                    route.original_router.routes,
                    prefix + route.include_context.prefix,
                )
            elif hasattr(route, "path"):
                yield prefix + route.path, route

    routes = list(flatten(main.app.routes))
    exceptions = {"/api/v1/login", "/api/v1/health", "/api/v1/ready"}
    independent = {
        "/api/v1/k8s/pods/{namespace}/{name}/files/download": (
            __import__(
                "web.api.pod_operations", fromlist=["download_identity"]
            ).download_identity
        ),
    }
    assert not any(
        path == "/api/v1/users/{user_id}"
        and "DELETE" in getattr(route, "methods", ())
        for path, route in routes
    )
    checked = 0
    for path, route in routes:
        if not path.startswith("/api/v1/") or not hasattr(route, "methods"):
            continue
        checked += 1
        if path in exceptions:
            continue
        if path in independent:
            assert any(
                dependency.call is independent[path]
                for dependency in route.dependant.dependencies
            )
        else:
            assert hasattr(route.endpoint, "__admin_only__"), path
            if not route.endpoint.__admin_only__:
                assert path in {
                    "/api/v1/me",
                    "/api/v1/me/password",
                    "/api/v1/logout",
                    "/api/v1/protected/unified",
                }
    assert checked >= 60


@pytest.mark.asyncio
async def test_identity_database_failure_returns_503_without_exposing_sql(
    store, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    def unavailable(username):
        raise OperationalError(
            "SQL with private hash", {}, Exception("private")
        )

    monkeypatch.setattr(auth, "load_auth_user", unavailable)
    async with client() as http:
        response = await http.post(
            "/api/v1/login", json={"username": "admin", "password": PASSWORD}
        )
    assert response.status_code == 503
    assert "private" not in response.text


@pytest.mark.asyncio
async def test_revoked_actor_and_stale_password_cannot_commit_identity_writes(
    store,
):
    second = add_user("platform_admin", "second")
    before = load_auth_user("second")
    admin_id = load_auth_user("admin")["user_id"]
    update(admin_id, second, role="readonly")
    with pytest.raises(identity.IdentityError) as error:
        identity.create_user(
            second["user_id"], "third", "hash", "readonly", [], []
        )
    assert error.value.code == 403
    identity.change_password(
        second["user_id"], "replacement-hash", actor_id=admin_id
    )
    with pytest.raises(identity.IdentityError) as error:
        identity.change_password(
            second["user_id"],
            "stale-write",
            expected_hash=before["password_hash"],
        )
    assert error.value.code == 401


@pytest.mark.asyncio
async def test_websockets_reject_non_admin_before_cluster_access(store):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    add_user()
    token = headers("reader")["Authorization"]

    # 同步 TestClient 在工作线程运行，避免阻塞测试的事件循环。
    def check():
        http = TestClient(main.app)
        with http.websocket_connect("/api/v1/ws?token=" + token) as socket:
            with pytest.raises(WebSocketDisconnect) as error:
                socket.receive_json()
            assert error.value.code == 1008
        with http.websocket_connect(
            "/api/v1/k8s/pods/team-a/demo/terminal?container=main"
        ) as socket:
            socket.send_json({"type": "auth", "token": token})
            value = socket.receive_json()
            assert value["type"] == "error"
            assert "授权" in value["message"]

    await asyncio.to_thread(check)
