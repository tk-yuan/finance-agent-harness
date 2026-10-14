"""会话管理接口：列表、新建、历史消息、删除。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...services import conversation_service

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("", summary="会话列表")
def list_conversations() -> dict:
    return {"items": conversation_service.list_conversations()}


@router.post("", summary="新建会话")
def create_conversation() -> dict:
    return conversation_service.create_conversation()


@router.get("/{conversation_id}/messages", summary="某会话的历史消息")
def get_messages(conversation_id: str) -> dict:
    return {"items": conversation_service.get_messages(conversation_id)}


@router.delete("/{conversation_id}", summary="删除会话")
def delete_conversation(conversation_id: str) -> dict:
    conversation_service.delete_conversation(conversation_id)
    return {"ok": True}
