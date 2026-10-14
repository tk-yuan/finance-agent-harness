"""会话与消息管理。

作用有两个：
1. 前端能看到历史会话列表、点进去看聊天记录
2. 给 Agent 提供「上下文」——每次提问带上最近几条消息，它才记得刚才说了什么
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select

from ..domain.db.models import Conversation, Message
from ..domain.db.session import SessionLocal

# 带给模型的历史消息条数（太多会撑爆上下文、变慢）
HISTORY_LIMIT = 10


def create_conversation(title: str = "新对话") -> dict:
    """新建一个会话。"""
    conv_id = uuid.uuid4().hex[:12]
    with SessionLocal() as db:
        conv = Conversation(id=conv_id, title=title[:100])
        db.add(conv)
        db.commit()
        return conv.as_dict()


def list_conversations(limit: int = 50) -> list[dict]:
    """列出所有会话，按最近更新排序。"""
    with SessionLocal() as db:
        rows = db.scalars(
            select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit)
        ).all()
    return [c.as_dict() for c in rows]


def get_messages(conversation_id: str, limit: int = 200) -> list[dict]:
    """取某个会话的消息记录。"""
    with SessionLocal() as db:
        rows = db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id)
            .limit(limit)
        ).all()
    return [m.as_dict() for m in rows]


def delete_conversation(conversation_id: str) -> None:
    """删除会话及其消息。"""
    with SessionLocal() as db:
        db.execute(delete(Message).where(Message.conversation_id == conversation_id))
        db.execute(delete(Conversation).where(Conversation.id == conversation_id))
        db.commit()


def ensure_conversation(conversation_id: str) -> None:
    """会话不存在就自动建一个（前端首次发消息时用）。"""
    with SessionLocal() as db:
        if db.get(Conversation, conversation_id) is None:
            db.add(Conversation(id=conversation_id, title="新对话"))
            db.commit()


def add_message(conversation_id: str, role: str, content: str,
                *, auto_title: bool = False) -> None:
    """存一条消息。若是该会话的第一条用户消息，顺便用它当标题。"""
    with SessionLocal() as db:
        db.add(Message(
            conversation_id=conversation_id,
            role=role,
            content=content[:8000],
        ))
        conv = db.get(Conversation, conversation_id)
        if conv:
            if auto_title and role == "user" and conv.title == "新对话":
                conv.title = content.strip()[:24] or "新对话"
            # 手动 touch，触发 updated_at
            from datetime import datetime

            conv.updated_at = datetime.now()
        db.commit()


def load_history(conversation_id: str, limit: int = HISTORY_LIMIT) -> list[dict]:
    """取最近几条消息，转成模型能吃的格式（作为对话上下文）。"""
    with SessionLocal() as db:
        rows = db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
            .limit(limit)
        ).all()
    rows.reverse()
    return [
        {"role": m.role, "content": m.content}
        for m in rows
        if m.role in ("user", "assistant")
    ]
