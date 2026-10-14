"""智能出纳：登记收付款单据（不管科目）。

为什么要和会计分开（《会计法》的不相容职务分离原则）：
    出纳管钱，会计管账。一个人既管钱又管账，挪用了公款自己改账就没人发现。
    所以本模块只负责"钱怎么动的、票在哪"，**不决定记到哪个科目**——
    那是会计的职责（见 app/api/v1/accounting.py）。

出纳录入的单据状态是 pending（待审核），只有会计审核通过后才计入报表。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from ...domain.db.models import LedgerEntry
from ...domain.db.session import SessionLocal
from ...services import stats_service

router = APIRouter(prefix="/entries", tags=["cashier"])

# 允许的凭证文件类型
ALLOWED_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png", ".ofd", ".webp"}
MAX_FILE_MB = 10
ATTACHMENT_DIR = Path("data/attachments")

# 出纳能选的非发票凭证类型（有些支出根本没有发票）
VOUCHER_TYPES = [
    ("invoice", "增值税发票", "采购货物、服务"),
    ("receipt", "收据 / 收款凭证", "付给个人的小额支出（500元以下）"),
    ("bank", "银行回单", "银行手续费、转账凭证"),
    ("payroll", "工资表", "发放工资（内部凭证）"),
    ("internal", "内部凭证", "折旧计算表、成本核算表等"),
]


class ManualEntry(BaseModel):
    entry_type: str = Field(..., description="income / expense")
    amount: float = Field(..., gt=0, le=100_000_000)
    description: str = Field(..., min_length=1, max_length=100)
    date: str | None = None
    payment: str = Field(default="银行存款")


@router.get("/options", summary="出纳录入的可选项")
def options() -> dict:
    from ...services import counterparty_service

    return {
        "payments": ["库存现金", "银行存款"],
        # 往来单位从档案表来，记账时下拉选，比手打摘要准
        "customers": [c for c in counterparty_service.list_counterparties()
                      if c["kind"] == "customer"],
        "suppliers": [c for c in counterparty_service.list_counterparties()
                      if c["kind"] == "supplier"],
        # 凭证类型：不是所有支出都有发票（工资用工资表、银行费有回单）
        "voucher_types": [{"value": v, "label": l, "hint": h}
                          for v, l, h in VOUCHER_TYPES],
        "note": "出纳只登记收付款和凭证，科目由会计审核时确定。",
    }


@router.post("/parse-invoice", summary="上传发票，自动识别字段（不记账，只返回识别结果）")
async def parse_invoice(voucher: UploadFile = File(..., description="发票图片或 PDF")):
    """识别发票内容，返回可自动填表的结构化字段。

    注意：**只识别、不记账**。识别结果返回前端填进表单，
    由人工核对确认后才真正入库——AI 识别会出错，必须有人把关。
    """
    from ...services.invoice_service import parse_invoice as _parse

    filename = voucher.filename or "invoice"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(400, f"不支持的格式 {suffix}，请上传图片或 PDF")

    content = await voucher.read()
    if not content:
        raise HTTPException(400, "文件为空")
    if len(content) > MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(400, f"文件过大（超过 {MAX_FILE_MB}MB）")

    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ATTACHMENT_DIR / f"_tmp_{uuid.uuid4().hex[:8]}{suffix}"
    tmp.write_bytes(content)
    try:
        result = _parse(tmp)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"发票识别失败：{exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)

    # 用发票上的销售方/购买方去档案里找匹配的往来单位，前端可直接选中
    from ...services import counterparty_service

    result["seller_match"] = None
    result["buyer_match"] = None
    for key, field in (("seller", "seller_match"), ("buyer", "buyer_match")):
        name = result.get(key)
        if not name:
            continue
        cp = counterparty_service.find_by_name(name)
        if cp:
            result[field] = {"id": cp.id, "name": cp.name,
                             "kind": cp.as_dict()["kind_cn"]}

    return {"ok": True, "fields": result}


@router.post("", summary="登记一笔收付款（提交待审核）")
async def create_entry(
    entry_type: str = Form(..., description="income（收款）/ expense（付款）"),
    amount: float = Form(..., gt=0, description="金额"),
    description: str = Form(..., min_length=1, max_length=100, description="摘要"),
    date: str | None = Form(default=None, description="日期 YYYY-MM-DD"),
    payment: str = Form(default="银行存款", description="收付款方式"),
    voucher_type: str = Form(default="invoice", description="凭证类型"),
    counterparty_id: int | None = Form(default=None, description="往来单位编号"),
    counterparty_name: str | None = Form(default=None,
                                         description="往来单位名称（档案里没有时自动建档）"),
    voucher: UploadFile = File(..., description="凭证附件，必传"),
):
    """出纳登记收付款。

    **不选科目**——出纳不懂也不该决定科目，那是会计审核时的事。
    录入后状态是 pending，等会计审核。
    """
    # ---- 1. 凭证校验：没有凭证不让登记 ----
    filename = voucher.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            400,
            f"凭证格式不支持（{suffix or '未知'}）。"
            f"请上传：{'/'.join(sorted(ALLOWED_SUFFIXES))}",
        )
    content = await voucher.read()
    if not content:
        raise HTTPException(400, "凭证文件为空，请重新上传。")
    if len(content) > MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(400, f"凭证文件过大（超过 {MAX_FILE_MB}MB）。")

    if entry_type not in ("income", "expense"):
        raise HTTPException(400, "收付方向只能是 income（收款）或 expense（付款）")

    entry_date = date or datetime.now().strftime("%Y-%m-%d")

    # ---- 2. 往来单位：填了名字但没选档案 → 自动建档 ----
    # 出纳不该为"这家单位还没建档"这种小事停下来
    if counterparty_id is None and counterparty_name:
        from ...services import counterparty_service

        existing = counterparty_service.find_by_name(counterparty_name)
        if existing:
            counterparty_id = existing.id
        else:
            kind = "customer" if entry_type == "income" else "supplier"
            created = counterparty_service.create(name=counterparty_name, kind=kind)
            if created.get("ok"):
                counterparty_id = created["counterparty"]["id"]

    # ---- 3. 保存凭证文件 ----
    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
    saved_name = f"{entry_date}_{uuid.uuid4().hex[:8]}{suffix}"
    (ATTACHMENT_DIR / saved_name).write_bytes(content)

    # ---- 3. 入库（科目留空，等会计审核）----
    with SessionLocal() as db:
        row = LedgerEntry(
            date=entry_date,
            amount=round(amount, 2),
            description=description.strip(),
            category="",            # 科目由会计确定
            entry_type=entry_type,
            debit_account="",       # 分录由会计生成
            credit_account="",
            counterparty_id=counterparty_id,
            attachment=saved_name,
            payment=payment,
            status="pending",       # 待审核
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        result = row.as_dict()

    direction = "收款" if entry_type == "income" else "付款"
    return {
        "ok": True,
        "entry": result,
        "message": f"已登记{direction}：{entry_date} {result['description']} "
                   f"{result['amount']:,.2f}元（凭证：{filename}）→ 待会计审核",
    }


@router.patch("/{entry_id}", summary="修改单据内容（被驳回后补正用）")
async def update_entry(
    entry_id: int,
    amount: float | None = Form(default=None),
    description: str | None = Form(default=None),
    date: str | None = Form(default=None),
    payment: str | None = Form(default=None),
    entry_type: str | None = Form(default=None),
    counterparty_id: int | None = Form(default=None),
    voucher: UploadFile | None = File(default=None),
):
    """出纳修改自己登记的单据。

    只有「待审核」和「已驳回」的单据能改——已入账的要走会计的反审核流程，
    否则出纳改一下就把会计审过的账改了，职务分离就白做了。
    """
    with SessionLocal() as db:
        row = db.get(LedgerEntry, entry_id)
        if row is None:
            raise HTTPException(404, f"找不到编号 {entry_id} 的单据")
        if row.status == "approved":
            raise HTTPException(
                400, "这笔已入账，出纳不能直接改。请联系会计反审核后再修改。")

        if amount is not None:
            if amount <= 0:
                raise HTTPException(400, "金额必须大于 0")
            row.amount = round(amount, 2)
        if description:
            row.description = description.strip()[:100]
        if date:
            row.date = date
        if payment:
            row.payment = payment
        if entry_type in ("income", "expense"):
            row.entry_type = entry_type
        if counterparty_id is not None:
            row.counterparty_id = counterparty_id

        # 换了凭证就替换文件
        if voucher is not None and voucher.filename:
            suffix = Path(voucher.filename).suffix.lower()
            if suffix not in ALLOWED_SUFFIXES:
                raise HTTPException(400, f"凭证格式不支持：{suffix}")
            content = await voucher.read()
            if not content:
                raise HTTPException(400, "凭证文件为空")
            ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
            new_name = f"{row.date}_{uuid.uuid4().hex[:8]}{suffix}"
            (ATTACHMENT_DIR / new_name).write_bytes(content)
            old = row.attachment
            row.attachment = new_name
            if old:
                (ATTACHMENT_DIR / Path(old).name).unlink(missing_ok=True)

        # 补正后自动回到待审核队列
        if row.status == "rejected":
            row.status = "pending"
            row.reject_reason = ""
        db.commit()
        db.refresh(row)
        result = row.as_dict()

    return {"ok": True, "entry": result,
            "message": f"已修改并重新提交审核：{result['description']} "
                       f"{result['amount']:,.2f}元"}


@router.delete("/{entry_id}", summary="删除一笔单据")
def delete_entry(entry_id: int) -> dict:
    result = stats_service.delete_entry(entry_id)
    if not result["ok"]:
        raise HTTPException(404, result["message"])
    return result


@router.get("/{entry_id}/voucher", summary="查看某笔单据的凭证原件")
def get_voucher(entry_id: int):
    """返回该笔单据对应的凭证文件。

    安全要点：只允许读取 data/attachments 目录下的文件，
    并校验文件名——防止通过 ../ 之类的路径穿越读到系统文件。
    """
    from fastapi.responses import FileResponse

    with SessionLocal() as db:
        row = db.get(LedgerEntry, entry_id)
        if row is None:
            raise HTTPException(404, f"找不到编号 {entry_id} 的单据")
        if not row.attachment:
            raise HTTPException(404, "这笔单据没有上传凭证")

        # 只取文件名部分，丢弃任何路径成分（防路径穿越）
        safe_name = Path(row.attachment).name
        path = (ATTACHMENT_DIR / safe_name).resolve()
        if not path.is_file() or ATTACHMENT_DIR.resolve() not in path.parents:
            raise HTTPException(404, "凭证文件不存在或已被删除")

    return FileResponse(path, filename=safe_name)
