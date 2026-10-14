"""对话接口。

支持**上传凭证**：用户在聊天框传了发票/收据，Agent 才能调用
record_with_voucher 记账（登记为待审核，仍要过会计审核）。
没传凭证时 Agent 会拒绝记账。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from ...api.deps import get_chat_service
from ...config import DATA_DIR
from ...schemas.chat import ChatResponse
from ...services.chat_service import ChatService

router = APIRouter(prefix="/chat", tags=["chat"])

# 绝对路径：相对路径会跟着启动目录跑，从别的目录起服务就写到别处去了
ATTACHMENT_DIR = DATA_DIR / "attachments"
ALLOWED_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png", ".ofd", ".webp"}
MAX_FILE_MB = 10


@router.post("", response_model=ChatResponse, summary="与财务助手对话（可带凭证）")
async def chat(
    message: str = Form(..., min_length=1, max_length=4000),
    session_id: str = Form(default="default"),
    voucher: UploadFile | None = File(default=None),
    service: ChatService = Depends(get_chat_service),
) -> ChatResponse:
    voucher_name = ""

    # 有上传文件就落盘，并把文件名告诉 Agent
    if voucher is not None and voucher.filename:
        suffix = Path(voucher.filename).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(400, f"凭证格式不支持（{suffix}），请上传图片或 PDF")
        content = await voucher.read()
        if not content:
            raise HTTPException(400, "凭证文件为空")
        if len(content) > MAX_FILE_MB * 1024 * 1024:
            raise HTTPException(400, f"凭证文件过大（超过 {MAX_FILE_MB}MB）")

        ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
        voucher_name = f"chat_{uuid.uuid4().hex[:8]}{suffix}"
        (ATTACHMENT_DIR / voucher_name).write_bytes(content)

    return service.chat(message=message, session_id=session_id,
                        voucher_name=voucher_name)
