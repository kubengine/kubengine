from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base, scoped_session
from core.config import Application

DB_PATH = Path(f"{Application.ROOT_DIR}/config/sqlite.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DB_PATH.touch(exist_ok=True)
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

# 初始化引擎（先保留默认配置，后续优化）
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    pool_size=5,  # 默认值，后续调整
    max_overflow=10,  # 默认值，后续调整
    pool_timeout=30,  # 默认超时，后续调整
    pool_recycle=3600,  # 关键：自动回收闲置连接（避免长连接失效）
)

# 线程安全的session工厂（scoped_session是关键）
SessionLocal = scoped_session(
    sessionmaker(autocommit=False, autoflush=False, bind=engine)
)
# 基类
Base = declarative_base()


@contextmanager
def get_db():
    db = SessionLocal()
    try:
        yield db
        # 提交（如果有未提交的事务，也可以放到业务层）
        # db.commit()
    except Exception as e:
        db.rollback()
        raise e
    finally:
        # 必须关闭session，释放连接回池
        db.close()
        # 对于scoped_session，额外确保线程级回收（可选但推荐）
        SessionLocal.remove()
