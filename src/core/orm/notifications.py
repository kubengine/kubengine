"""
A durable invalidation revision shared by web and background processes.
"""

from sqlalchemy import Column, Integer, text

from core.orm.engine import Base, get_db


class ClusterRevision(Base):
    __tablename__ = "cluster_revision"
    id = Column(Integer, primary_key=True)
    revision = Column(Integer, nullable=False)


def publish_cluster_change():
    with get_db() as db:
        db.execute(
            text(
                "INSERT INTO cluster_revision (id, revision) VALUES (1, 1) "
                "ON CONFLICT(id) DO UPDATE SET revision = revision + 1"
            )
        )
        db.commit()


def read_cluster_revision():
    with get_db() as db:
        row = db.get(ClusterRevision, 1)
        return row.revision if row else 0
