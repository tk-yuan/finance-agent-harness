"""对话相关的数据契约。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """用户发来的一条消息。"""

    message: str = Field(..., min_length=1, max_length=4000, description="用户输入")
    session_id: str = Field(default="default", description="会话标识")


class ToolCall(BaseModel):
    """Agent 这次调用了哪个工具——前端用它显示"记账/查询"的标签。"""

    name: str
    label: str = ""      # 友好标签，如"记账""查询"
    arg: str = ""        # 关键参数摘要


class ChatResponse(BaseModel):
    """Agent 的回复。"""

    session_id: str
    answer: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    kind: str = "chat"   # record / query / reconcile / chat —— 前端据此显示不同样式
