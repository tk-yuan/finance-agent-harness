"""看图工具：让 Agent 真的能读图片和扫描件。

## 为什么需要它

用户往对话里传一张发票，之前的链路只把**文件名**递给 Agent——
它知道"有个文件叫 chat_xxx.jpg"，但**看不见里面是什么**，
于是只能回一句"请你自己核对金额"。

出纳页的发票识别（`/parse-invoice`）其实是好用的，但那段能力只长在那个接口里，
Agent 用不上。这里把它接进工具池，Agent 才真的长了眼睛。

## 支持什么

  · 图片（拍照的发票、扫描件、截图）→ 走视觉模型直接看
  · 带文字层的 PDF → 抽文字交给文本模型
  · 扫描版 PDF（没有文字层）→ 明确告诉它不行，让它请用户转成图片，**不猜**

## 权限

和 `read_file` 一样过 `PermissionPolicy`：工作区内的图直接看，
**区外的图（比如你桌面上的扫描件）会弹授权卡片问你**——
"能看图"不等于"能看你电脑上所有的图"。
"""

from __future__ import annotations

from pathlib import Path

from ..domain.exceptions import AppError
from ..runtime.permissions import PermissionDenied
from ..runtime.tools import Tool, ToolRegistry
from ..services.invoice_service import IMAGE_SUFFIXES, PDF_SUFFIX, ask_vision

_DEFAULT_QUESTION = (
    "描述这张图的内容。如果它是发票、收据或单据，"
    "把金额、日期、对方单位、商品或服务名称都读出来。"
)


def register_vision_tools(registry: ToolRegistry, policy) -> None:
    """注册看图工具。路径同样过权限层——区外要人授权。"""

    def read_image(path: str, question: str = "") -> dict:
        try:
            p = policy.check_path(path, tool="read_image")
        except PermissionDenied as e:
            return {"error": str(e)}
        if not p.exists():
            return {"error": f"文件不存在：{path}"}
        if p.suffix.lower() not in IMAGE_SUFFIXES | {PDF_SUFFIX}:
            return {"error": f"看不了 {p.suffix} 这种格式，只认图片(jpg/png/webp)和 PDF。"}

        q = (question or "").strip() or _DEFAULT_QUESTION
        try:
            text = ask_vision(p, q)
        except AppError as e:
            return {"error": str(e)}
        if not text.strip():
            return {"error": "模型没有返回内容，可能这张图看不清或不是单据。"}
        return {"path": str(p), "answer": text.strip()}

    registry.register(Tool(
        name="read_image",
        description=(
            "看一张图片或 PDF 的内容并回答关于它的问题。\n"
            "**用户上传凭证后必须先看再判断**——只凭文件名你不知道里面写了什么，"
            "更不该猜金额和日期。\n"
            "适用：发票/收据/回单的图片、对账单截图、扫描件 PDF。\n"
            "注意：扫描版 PDF（没有文字层）读不了，要请用户截图成图片再传。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "图片或 PDF 的路径。用户刚上传的凭证直接用提示里给的文件名。"},
                "question": {"type": "string",
                             "description": "想问什么。不填就默认读出单据的关键字段。"},
            },
            "required": ["path"],
        },
        handler=read_image,
        read_only=True,
        category="vision",
    ))
