"""Persist token revocation across API processes and service restarts."""

import hashlib
import time

from sqlalchemy import Column, Integer, String, text
from sqlalchemy.dialects.sqlite import insert

from core.orm.engine import Base, get_db


class RevokedToken(Base):
    __tablename__ = "revoked_token"

    token_hash = Column(String(64), primary_key=True)
    expires_at = Column(Integer, nullable=False, index=True)


class AuthSession(Base):
    __tablename__ = "auth_session"

    session_id = Column(String(64), primary_key=True)
    username = Column(String, nullable=False)
    credential_version = Column(String(64), nullable=False)
    expires_at = Column(Integer, nullable=False, index=True)
    revoked_at = Column(Integer)


def issue_session(session_id: str, username: str, version: str, expires_at: int, *, renew: bool) -> None:
    with get_db() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        now = int(time.time())
        row = db.get(AuthSession, session_id)
        if renew:
            if (row is None or row.revoked_at is not None or row.expires_at <= now
                    or row.username != username or row.credential_version != version):
                raise ValueError("Login session expired or revoked")
            row.expires_at = max(row.expires_at, expires_at)
        else:
            db.add(AuthSession(session_id=session_id, username=username,
                               credential_version=version, expires_at=expires_at))
        db.query(AuthSession).filter(AuthSession.expires_at <= now).delete(synchronize_session=False)
        db.commit()


def session_active(session_id: str, username: str, version: str) -> bool:
    with get_db() as db:
        return db.query(AuthSession.session_id).filter_by(
            session_id=session_id, username=username, credential_version=version,
            revoked_at=None,
        ).filter(AuthSession.expires_at > int(time.time())).first() is not None


def revoke_session(session_id: str) -> None:
    with get_db() as db:
        db.query(AuthSession).filter_by(session_id=session_id).update(
            {"revoked_at": int(time.time())}, synchronize_session=False,
        )
        db.commit()


def revoke_token(token: str, expires_at: int) -> None:
    with get_db() as db:
        db.query(RevokedToken).filter(RevokedToken.expires_at <= int(time.time())).delete()
        db.execute(
            insert(RevokedToken).values(
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=expires_at,
            ).on_conflict_do_nothing(index_elements=["token_hash"])
        )
        db.commit()


def is_token_revoked(token: str) -> bool:
    with get_db() as db:
        return db.query(RevokedToken.token_hash).filter(
            RevokedToken.token_hash == hashlib.sha256(token.encode()).hexdigest(),
            RevokedToken.expires_at > int(time.time()),
        ).first() is not None
