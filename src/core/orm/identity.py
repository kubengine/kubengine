"""控制台身份存储。配置只在首次迁移读取，之后数据库是唯一认证来源。"""

import time
from uuid import uuid4

from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, text

from core.orm.auth import AuthSession
from core.orm.engine import Base, engine, get_db

ROLES = {
    "platform_admin": "平台管理员",
    "namespace_operator": "命名空间运维",
    "readonly": "只读用户",
}
SENSITIVE_PERMISSIONS = frozenset(
    {"secret.read", "pod.terminal", "pod.files.read", "pod.files.write"}
)
MIGRATION = "identity-v1"


class IdentityMigration(Base):
    __tablename__ = "identity_migration"

    name = Column(String, primary_key=True)
    completed_at = Column(Integer, nullable=False)


class BuiltinRole(Base):
    __tablename__ = "identity_role"

    name = Column(String, primary_key=True)
    label = Column(String, nullable=False)


class ConsoleUser(Base):
    __tablename__ = "console_user"

    user_id = Column(String(32), primary_key=True)
    username = Column(String(64), nullable=False, unique=True)
    password_hash = Column(String, nullable=False)
    # 仅保留旧哈希/标识以兼容迁移；旧 AK/SK 协议继续关闭。
    ak = Column(String, nullable=False, default="")
    sk_hash = Column(String, nullable=False, default="")
    enabled = Column(Boolean, nullable=False, default=True)
    role = Column(String, ForeignKey("identity_role.name"), nullable=False)
    auth_revision = Column(Integer, nullable=False, default=1)
    revision = Column(Integer, nullable=False, default=1)
    created_at = Column(Integer, nullable=False)
    updated_at = Column(Integer, nullable=False)


class NamespaceBinding(Base):
    __tablename__ = "identity_namespace"

    user_id = Column(
        String(32), ForeignKey("console_user.user_id"), primary_key=True
    )
    namespace = Column(String(63), primary_key=True)


class SensitiveGrant(Base):
    __tablename__ = "identity_sensitive_grant"

    user_id = Column(
        String(32), ForeignKey("console_user.user_id"), primary_key=True
    )
    permission = Column(String(64), primary_key=True)


class IdentityError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def ensure_identity_schema():
    """在同一 SQLite 写锁内建表并迁移，多个进程可安全重复执行。"""
    from core.auth_credentials import load_config_auth_users

    with engine.begin() as connection:
        connection.execute(text("BEGIN IMMEDIATE"))
        for model in (
            IdentityMigration,
            BuiltinRole,
            ConsoleUser,
            NamespaceBinding,
            SensitiveGrant,
            AuthSession,
        ):
            model.__table__.create(connection, checkfirst=True)
        for name, label in ROLES.items():
            connection.execute(
                text(
                    "INSERT OR IGNORE INTO identity_role (name, label) "
                    "VALUES (:name, :label)"
                ),
                {"name": name, "label": label},
            )
        if connection.execute(
            text("SELECT name FROM identity_migration WHERE name=:name"),
            {"name": MIGRATION},
        ).first():
            return
        records = load_config_auth_users()
        if not records or any(
            not name or not record.get("password_hash")
            for name, record in records.items()
        ):
            raise RuntimeError("首次迁移需要配置至少一个带密码哈希的管理员")
        # 旧版所有配置用户都有平台管理能力，迁移保持已有访问语义。
        now = int(time.time())
        for username, record in records.items():
            connection.execute(
                ConsoleUser.__table__.insert().values(
                    user_id=uuid4().hex,
                    username=username,
                    password_hash=record["password_hash"],
                    ak=record.get("ak", ""),
                    sk_hash=record.get("sk_hash", ""),
                    role="platform_admin",
                    enabled=True,
                    auth_revision=1,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        connection.execute(
            IdentityMigration.__table__.insert().values(
                name=MIGRATION, completed_at=now
            )
        )


def _public_user(db, row):
    return {
        "user_id": row.user_id,
        "username": row.username,
        "enabled": row.enabled,
        "role": row.role,
        "namespaces": [
            item.namespace
            for item in db.query(NamespaceBinding)
            .filter_by(user_id=row.user_id)
            .order_by(NamespaceBinding.namespace)
        ],
        "sensitive_permissions": [
            item.permission
            for item in db.query(SensitiveGrant)
            .filter_by(user_id=row.user_id)
            .order_by(SensitiveGrant.permission)
        ],
        "revision": row.revision,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def authentication_record(username):
    with get_db() as db:
        # sqlite3 默认事务模式下 SELECT 不自动开启快照；避免拼出不同版本的授权。
        db.execute(text("BEGIN"))
        row = db.query(ConsoleUser).filter_by(username=username).first()
        if row is None:
            return None
        return {
            **_public_user(db, row),
            "password_hash": row.password_hash,
            "ak": row.ak,
            "sk_hash": row.sk_hash,
            "auth_revision": row.auth_revision,
        }


def list_users(page=1, page_size=20):
    with get_db() as db:
        query = db.query(ConsoleUser)
        total = query.count()
        rows = (
            query.order_by(ConsoleUser.created_at, ConsoleUser.username)
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {
            "items": [_public_user(db, row) for row in rows],
            "total": total,
        }


def _replace_grants(db, user_id, namespaces, sensitive_permissions):
    if not set(sensitive_permissions) <= SENSITIVE_PERMISSIONS:
        raise IdentityError(422, "未知敏感权限")
    db.query(NamespaceBinding).filter_by(user_id=user_id).delete()
    db.query(SensitiveGrant).filter_by(user_id=user_id).delete()
    db.add_all(
        NamespaceBinding(user_id=user_id, namespace=namespace)
        for namespace in sorted(set(namespaces))
    )
    db.add_all(
        SensitiveGrant(user_id=user_id, permission=permission)
        for permission in sorted(set(sensitive_permissions))
    )


def _assert_actor(db, actor_id):
    actor = db.get(ConsoleUser, actor_id)
    if actor is None or not actor.enabled:
        raise IdentityError(401, "登录身份已失效")
    if actor.role != "platform_admin":
        raise IdentityError(403, "需要平台管理员权限")


def create_user(actor_id, username, password_hash, role, namespaces, grants):
    if role not in ROLES:
        raise IdentityError(422, "未知角色")
    with get_db() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        _assert_actor(db, actor_id)
        if db.query(ConsoleUser).filter_by(username=username).first():
            raise IdentityError(409, "用户名已存在")
        now = int(time.time())
        row = ConsoleUser(
            user_id=uuid4().hex,
            username=username,
            password_hash=password_hash,
            role=role,
            enabled=True,
            auth_revision=1,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.flush()
        _replace_grants(db, row.user_id, namespaces, grants)
        db.flush()
        result = _public_user(db, row)
        db.commit()
        return result


def update_user(
    actor_id, user_id, revision, enabled, role, namespaces, grants
):
    if role not in ROLES:
        raise IdentityError(422, "未知角色")
    with get_db() as db:
        # SQLite 串行化所有身份写入，防止两个管理员并发移除最后的管理员。
        db.execute(text("BEGIN IMMEDIATE"))
        _assert_actor(db, actor_id)
        row = db.get(ConsoleUser, user_id)
        if row is None:
            raise IdentityError(404, "用户不存在")
        if row.revision != revision:
            raise IdentityError(409, "用户已被修改，请刷新后重试")
        if (
            row.enabled
            and row.role == "platform_admin"
            and (not enabled or role != "platform_admin")
        ):
            count = (
                db.query(ConsoleUser)
                .filter_by(enabled=True, role="platform_admin")
                .count()
            )
            if count <= 1:
                raise IdentityError(409, "必须保留至少一个可用的平台管理员")
        if row.enabled != enabled:
            _invalidate_sessions(db, row)
        row.enabled, row.role = enabled, role
        row.revision += 1
        row.updated_at = int(time.time())
        _replace_grants(db, user_id, namespaces, grants)
        db.flush()
        result = _public_user(db, row)
        db.commit()
        return result


def _invalidate_sessions(db, row):
    row.auth_revision += 1
    db.query(AuthSession).filter_by(username=row.username).update(
        {"revoked_at": int(time.time())}, synchronize_session=False
    )


def change_password(
    user_id, password_hash, *, actor_id=None, expected_hash=None
):
    with get_db() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        if actor_id is not None:
            _assert_actor(db, actor_id)
        row = db.get(ConsoleUser, user_id)
        if row is None:
            raise IdentityError(404, "用户不存在")
        if actor_id is None and (
            not row.enabled or row.password_hash != expected_hash
        ):
            raise IdentityError(401, "凭据已变更，请重新登录")
        row.password_hash = password_hash
        row.revision += 1
        row.updated_at = int(time.time())
        _invalidate_sessions(db, row)
        db.commit()


def reset_admin_password(password_hash):
    """本机 CLI 恢复入口，只重置已有的启用管理员，不自动重新启用用户。"""
    ensure_identity_schema()
    with get_db() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.query(ConsoleUser).filter_by(username="admin").first()
        if row is None or not row.enabled or row.role != "platform_admin":
            raise IdentityError(
                409, "admin 不是启用的平台管理员，请使用用户管理接口"
            )
        row.password_hash = password_hash
        row.revision += 1
        row.updated_at = int(time.time())
        _invalidate_sessions(db, row)
        db.commit()
