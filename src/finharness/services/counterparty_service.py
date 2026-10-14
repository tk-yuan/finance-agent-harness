"""往来单位档案：客户 / 供应商管理。

真实财务里，"这家客户跟我做了多少生意、还欠我多少"是最常被问的问题。
靠摘要里搜关键词做不到（名字写错就搜不到），所以要有独立的档案表，
记账时从下拉选，查询时按 ID 精确关联。
"""

from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy import select

from ..domain.db.models import Counterparty, LedgerEntry
from ..domain.db.session import SessionLocal

# 从摘要里提取往来单位名的模式
# 例：销售商品-石家庄恒信商贸  →  客户：石家庄恒信商贸
#     采购商品-浙江义乌小商品批发城 →  供应商：浙江义乌小商品批发城
_PATTERNS: list[tuple[str, str, str]] = [
    (r"^销售商品[-－](.+)$", "customer", "销售"),
    (r"^采购商品[-－](.+)$", "supplier", "采购"),
    (r"^紧急采购[-－](.+)$", "supplier", "采购"),
]


def _extract(description: str) -> tuple[str, str] | None:
    """从摘要里解析出 (单位名, 类型)。解析不出返回 None。"""
    desc = (description or "").strip()
    for pattern, kind, _label in _PATTERNS:
        m = re.match(pattern, desc)
        if m:
            name = m.group(1).strip()
            if name and len(name) <= 100:
                return name, kind
    return None


def sync_from_entries() -> dict:
    """扫描历史账目，自动建立往来单位档案并回填关联。

    这是"数据迁移"：老数据把单位名写在摘要里，现在抽出来建成档案。
    幂等——重复执行不会重复建，只会补上遗漏的关联。
    """
    with SessionLocal() as db:
        rows = db.scalars(select(LedgerEntry)).all()
        existing = {c.name: c for c in db.scalars(select(Counterparty)).all()}

        created, linked = 0, 0
        for row in rows:
            parsed = _extract(row.description)
            if not parsed:
                continue
            name, kind = parsed

            cp = existing.get(name)
            if cp is None:
                cp = Counterparty(name=name, kind=kind)
                db.add(cp)
                db.flush()          # 拿到自增 id
                existing[name] = cp
                created += 1

            if row.counterparty_id != cp.id:
                row.counterparty_id = cp.id
                linked += 1

        db.commit()

    return {"created": created, "linked": linked,
            "total_counterparties": len(existing)}


def list_counterparties() -> list[dict]:
    """往来单位列表（不含统计）。"""
    with SessionLocal() as db:
        rows = db.scalars(select(Counterparty).order_by(Counterparty.name)).all()
    return [c.as_dict() for c in rows]


def _stats(cp: Counterparty, entries: list[LedgerEntry]) -> dict:
    """算某个单位的往来统计。"""
    mine = [e for e in entries if e.counterparty_id == cp.id]
    if not mine:
        return {"count": 0, "amount": 0.0, "credit_amount": 0.0,
                "last_date": "", "balance_hint": 0.0}

    total = sum(e.amount for e in mine)

    # 挂账金额：客户看「应收账款」，供应商看「应付账款」
    if cp.kind == "customer":
        credit = sum(e.amount for e in mine if e.debit_account == "应收账款")
        balance = credit            # 累计赊销（尚未逐笔核销）
    else:
        credit = sum(e.amount for e in mine if e.credit_account == "应付账款")
        balance = credit

    return {
        "count": len(mine),
        "amount": round(total, 2),
        "credit_amount": round(credit, 2),
        "balance_hint": round(balance, 2),
        "last_date": max(e.date for e in mine),
    }


def list_with_stats() -> list[dict]:
    """往来单位列表 + 往来统计（只算已入账的账目）。"""
    with SessionLocal() as db:
        cps = db.scalars(select(Counterparty).order_by(Counterparty.name)).all()
        entries = [r for r in db.scalars(select(LedgerEntry)).all()
                   if r.status == "approved"]

    out = []
    for cp in cps:
        d = cp.as_dict()
        d.update(_stats(cp, entries))
        out.append(d)
    # 交易额大的排前面
    return sorted(out, key=lambda x: -x["amount"])


def get_detail(counterparty_id: int) -> dict:
    """某个单位的详细往来：基本信息 + 统计 + 明细列表。"""
    with SessionLocal() as db:
        cp = db.get(Counterparty, counterparty_id)
        if cp is None:
            return {"ok": False, "message": f"找不到编号 {counterparty_id} 的往来单位"}
        rows = db.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.counterparty_id == counterparty_id,
                   LedgerEntry.status == "approved")
            .order_by(LedgerEntry.date)
        ).all()
        entries = [r.as_dict() for r in rows]
        info = cp.as_dict()
        stat = _stats(cp, rows)

    return {"ok": True, "info": info, "stats": stat, "entries": entries}


def find_by_name(name: str) -> Counterparty | None:
    """按名称找往来单位。先精确匹配，再尝试包含匹配（"恒信" → "石家庄恒信商贸"）。

    发票上印的是全称，档案里可能是简称，所以要有模糊兜底。
    """
    if not name or not name.strip():
        return None
    key = name.strip()
    with SessionLocal() as db:
        cps = db.scalars(select(Counterparty)).all()
        # 1. 完全相同
        for c in cps:
            if c.name == key:
                return c
        # 2. 互相包含（去掉"有限公司"这类后缀再比一次，命中率更高）
        def norm(s: str) -> str:
            for suffix in ("有限公司", "有限责任公司", "公司", "厂", "店"):
                s = s.replace(suffix, "")
            return s.strip()

        nk = norm(key)
        for c in cps:
            nc = norm(c.name)
            if nc and (nc in nk or nk in nc):
                return c
    return None


def create(name: str, kind: str = "customer", contact: str = "",
           phone: str = "", note: str = "") -> dict:
    name = name.strip()
    if not name:
        return {"ok": False, "message": "名称不能为空"}
    with SessionLocal() as db:
        if db.scalars(select(Counterparty).where(Counterparty.name == name)).first():
            return {"ok": False, "message": f"「{name}」已存在"}
        cp = Counterparty(name=name, kind=kind, contact=contact, phone=phone, note=note)
        db.add(cp)
        db.commit()
        db.refresh(cp)
        return {"ok": True, "counterparty": cp.as_dict()}


def update(counterparty_id: int, **fields) -> dict:
    with SessionLocal() as db:
        cp = db.get(Counterparty, counterparty_id)
        if cp is None:
            return {"ok": False, "message": "找不到该往来单位"}
        for k, v in fields.items():
            if v is not None and hasattr(cp, k):
                setattr(cp, k, v)
        db.commit()
        db.refresh(cp)
        return {"ok": True, "counterparty": cp.as_dict()}


def delete(counterparty_id: int) -> dict:
    with SessionLocal() as db:
        cp = db.get(Counterparty, counterparty_id)
        if cp is None:
            return {"ok": False, "message": "找不到该往来单位"}
        n = len(db.scalars(
            select(LedgerEntry).where(LedgerEntry.counterparty_id == counterparty_id)
        ).all())
        if n > 0:
            return {"ok": False,
                    "message": f"该单位还有 {n} 笔关联账目，不能删除。"
                               f"请先处理这些账目。"}
        db.delete(cp)
        db.commit()
    return {"ok": True, "message": "已删除"}
