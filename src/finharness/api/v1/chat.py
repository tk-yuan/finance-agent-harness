"""对话接口。

## 为什么是异步的（提交任务 + 事件流），不是一问一答

Agent 跑到一半可能要**停下来等人**：

  · 要入账 → 会计得点头（`post_voucher` 走人工审批）
  · 要跑命令 → 得人看清命令再允许（`run_command`）
  · 要读写工作区外的文件 → 得人授权
  · 科目拿不准 → 得问业务性质（`ask_user`）

同步接口做不了这些：HTTP 请求不能挂在那儿等几分钟。所以这里是

    POST /api/v1/chat                        → {run_id}      提交
    GET  /runs/{run_id}/events               → SSE          跟着看
    POST /runs/{run_id}/approvals/{id}       → {}           允许 / 拒绝 / 回答

会话历史落在数据库（`conversation_service`），由这里读出来交给运行时、
结论再由回调写回去——运行时不持有业务会话，两边不搞两份会漂移的历史。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ...config import DATA_DIR
from ...server.service import shared_manager
from ...services import conversation_service

router = APIRouter(prefix="/chat", tags=["chat"])

# 绝对路径：相对路径会跟着启动目录跑，从别的目录起服务就写到别处去了
ATTACHMENT_DIR = DATA_DIR / "attachments"
ALLOWED_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png", ".ofd", ".webp"}
MAX_FILE_MB = 10


@router.post("", summary="向财务助手提问（异步，中途可人工介入）")
async def chat(
    message: str = Form(..., min_length=1, max_length=4000),
    session_id: str = Form(default="default"),
    voucher: UploadFile | None = File(default=None),
) -> dict:
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

    conversation_service.ensure_conversation(session_id)
    history = conversation_service.load_history(session_id)

    content = message
    if voucher_name:
        content = (
            f"{message}\n\n"
            f"〔系统信息：用户本次上传了凭证，文件名为 {voucher_name}。"
            f"如果用户要求记账，调用 submit_voucher 时 voucher 参数填这个文件名。〕"
        )
    conversation_service.add_message(session_id, "user", content, auto_title=True)

    def on_done(rec) -> None:
        """运行结束（成功或失败）都把结论写回会话，界面刷新就能看到。"""
        if rec.answer:
            conversation_service.add_message(session_id, "assistant", rec.answer)
        elif rec.error:
            conversation_service.add_message(
                session_id, "assistant", f"（运行失败：{rec.error}）")

    rec = shared_manager().start(
        content, conversation_id=session_id, history=history, on_done=on_done)

    return {
        "run_id": rec.run_id,
        "conversation_id": session_id,
        "turn": rec.turn,
        "status": rec.status,
        # 事件流地址一并给出，前端不用自己拼
        "events_url": f"/runs/{rec.run_id}/events",
    }
