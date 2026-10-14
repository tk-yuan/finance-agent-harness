"""数据库会话：给工具层用的最小封装。

用 SQLite（项目内文件），克隆下来零配置即可跑。
真实部署换成 PostgreSQL 只需改 `DB_PATH` 处的 URL——业务代码不受影响。
"""

from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ...config import DB_PATH
from .base import Base
from . import models  # noqa: F401  —— 导入才注册到 Base.metadata

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_engine = create_engine(f"sqlite:///{DB_PATH}", future=True)
_Session = sessionmaker(bind=_engine, expire_on_commit=False, class_=Session)


def init_db() -> None:
    """建表。幂等，可以反复调。"""
    Base.metadata.create_all(_engine)


@contextmanager
def session():
    """with session() as db: ... —— 退出时自动提交，异常自动回滚。"""
    db = _Session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
