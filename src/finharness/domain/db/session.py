"""数据库会话与建表。

FastAPI 的同步请求跑在线程池里，所以 SQLite 必须 `check_same_thread=False`，
否则一个请求换个线程就报「SQLite objects created in a thread can only be used
in that same thread」。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ...config import DB_PATH

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(f"sqlite:///{DB_PATH}",
                       connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False,
                            expire_on_commit=False, class_=Session)


def get_db():
    """FastAPI 依赖：每个请求一个独立会话，用完关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """建表（幂等）。"""
    from . import models  # noqa: F401  确保模型注册到 metadata
    from .base import Base

    Base.metadata.create_all(bind=engine)


@contextmanager
def session():
    """脚本/工具里用的上下文管理器：退出自动提交，异常回滚。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
