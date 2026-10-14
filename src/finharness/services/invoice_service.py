"""发票识别：上传发票 → 自动提取字段。

这是「智能出纳」的核心：出纳原来要对着发票一个字一个字敲，
现在拍张照/传个文件，系统自动把金额、日期、开票方、商品都填好，
人只需要核对一眼。

两条识别路径：
- 图片（拍照的发票、扫描件）→ 走视觉模型 Qwen3-VL 直接看图
- PDF 电子发票 → 优先抽文字层（快且免费），交给文本模型结构化
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from openai import OpenAI

from ..config import Settings, get_settings
from ..domain.exceptions import AppError
from ..services.bill_importer import categorize

# 视觉模型：看图识发票
VL_MODEL = "Qwen/Qwen3-VL-8B-Instruct"

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
PDF_SUFFIX = ".pdf"

_EXTRACT_PROMPT = """这是一张发票（或发票照片）。请提取以下信息，**只输出 JSON**，不要任何解释、不要 markdown 代码块：

{
  "invoice_no": "发票号码（没有就填空字符串）",
  "date": "开票日期，格式 YYYY-MM-DD（没有就填空字符串）",
  "seller": "销售方/开票方名称",
  "buyer": "购买方名称",
  "amount": 价税合计金额，纯数字（如 1130.00）,
  "items": "商品或服务名称，多个用顿号分隔",
  "invoice_type": "发票类型（如 增值税电子普通发票）"
}

注意：amount 一定是数字类型，不要带￥符号和千分位逗号。"""


def _client(settings: Settings) -> OpenAI:
    return OpenAI(
        api_key=settings.siliconflow_api_key,
        base_url=settings.siliconflow_base_url,
    )


def _parse_json(text: str) -> dict:
    """从模型输出里抠出 JSON（模型有时会裹一层 markdown 代码块）。"""
    text = text.strip()
    # 去掉 ```json ... ``` 包裹
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    # 截取第一个 { 到最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AppError(f"发票识别结果无法解析：{exc}") from exc


def _to_float(value) -> float:
    """把 '1,130.00'、'￥1130' 这类值转成 float。"""
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d.\-]", "", str(value or ""))
    try:
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        return 0.0


def _normalize(raw: dict) -> dict:
    """规整模型输出，并给出建议科目。"""
    items = str(raw.get("items") or "").strip()
    seller = str(raw.get("seller") or "").strip()
    amount = _to_float(raw.get("amount"))

    # 用「商品名称 + 销售方」一起判断科目，命中率更高
    guessed = categorize(f"{items} {seller}")
    if guessed == "其他":
        # 兜底：办公用品/文具类常见关键词再试一次
        if any(k in items for k in ["纸", "笔", "文具", "打印", "耗材", "办公"]):
            guessed = "办公费"

    return {
        "invoice_no": str(raw.get("invoice_no") or "").strip(),
        "date": str(raw.get("date") or "").strip(),
        "seller": seller,
        "buyer": str(raw.get("buyer") or "").strip(),
        "amount": round(amount, 2),
        "items": items,
        "invoice_type": str(raw.get("invoice_type") or "").strip(),
        "suggested_category": guessed,
        # 给用户的提醒：哪些地方需要人工核对
        "warnings": _warnings(raw, amount),
    }


def _warnings(raw: dict, amount: float) -> list[str]:
    """生成"需要人工确认"的提示——识别结果必须人来核对，不能全信。"""
    out = []
    if amount <= 0:
        out.append("金额识别失败，请手动填写")
    if not str(raw.get("invoice_no") or "").strip():
        out.append("未识别到发票号码")
    if not str(raw.get("date") or "").strip():
        out.append("未识别到开票日期")
    if not str(raw.get("seller") or "").strip():
        out.append("未识别到销售方名称")
    return out


def parse_invoice(file_path: str | Path, settings: Settings | None = None) -> dict:
    """识别发票，返回结构化字段 + 建议科目 + 待核对提示。"""
    settings = settings or get_settings()
    path = Path(file_path)
    if not path.exists():
        raise AppError(f"文件不存在：{path}")

    suffix = path.suffix.lower()
    client = _client(settings)

    if suffix in IMAGE_SUFFIXES:
        b64 = base64.b64encode(path.read_bytes()).decode()
        mime = "image/jpeg" if suffix in {".jpg", ".jpeg"} else f"image/{suffix.lstrip('.')}"
        content = [
            {"type": "text", "text": _EXTRACT_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]
        resp = client.chat.completions.create(
            model=VL_MODEL,
            messages=[{"role": "user", "content": content}],
            max_tokens=600,
        )
        raw = _parse_json(resp.choices[0].message.content or "")

    elif suffix == PDF_SUFFIX:
        from pypdf import PdfReader

        try:
            text = "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
        except Exception as exc:
            raise AppError(f"PDF 解析失败：{exc}") from exc

        if len(text.strip()) < 20:
            raise AppError(
                "这个 PDF 是扫描件、没有文字层，无法直接识别。"
                "请把它截图成图片（jpg/png）后再上传。"
            )
        resp = client.chat.completions.create(
            model=settings.siliconflow_chat_model,
            messages=[{
                "role": "user",
                "content": f"{_EXTRACT_PROMPT}\n\n发票文字内容如下：\n{text[:3000]}",
            }],
            max_tokens=600,
        )
        raw = _parse_json(resp.choices[0].message.content or "")
    else:
        raise AppError(f"不支持的格式 {suffix}，请上传图片(jpg/png)或 PDF。")

    return _normalize(raw)
