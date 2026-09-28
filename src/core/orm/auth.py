"""Persist token revocation across API processes and service restarts."""

import hashlib
import time

from sqlalchemy import Column, Integer, String
from sqlalchemy.dialects.sqlite import insert

from core.orm.engine import Base, get_db


class RevokedToken(Base):
    __tablename__ = "revoked_token"

    token_hash = Column(String(64), primary_key=True)
    expires_at = Column(Integer, nullable=False, index=True)


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
