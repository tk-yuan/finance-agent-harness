"""账单导入：把支付宝/微信导出的账单 CSV 解析进账本。

真实场景：企业/个人从支付宝、微信导出交易流水，自动分类入账。
这是「真实数据」的来源——不是编的，是用户自己的真实交易记录。
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from pathlib import Path

from ..domain.db.models import LedgerEntry
from ..domain.db.session import SessionLocal

# 商户关键词 → 科目 的映射规则（按顺序匹配，先匹配到的优先）
# 这是"自动分类"的核心：靠规则快速分类，比调 LLM 快且免费
CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("交通费", ["滴滴", "高德", "曹操", "T3", "花小猪", "地铁", "公交", "出租",
                "打车", "加油", "停车", "高速", "12306", "铁路", "航空", "共享单车", "哈啰"]),
    ("餐饮费", ["美团", "饿了么", "肯德基", "麦当劳", "星巴克", "瑞幸", "餐厅", "饭店",
                "火锅", "咖啡", "奶茶", "外卖", "食堂", "餐饮", "烧烤", "面馆", "小吃"]),
    ("差旅费", ["酒店", "宾馆", "民宿", "携程", "飞猪", "去哪儿", "住宿", "客栈"]),
    ("通讯费", ["中国移动", "中国联通", "中国电信", "话费", "流量", "宽带",
                "顺丰", "圆通", "中通", "申通", "韵达", "邮政", "快递"]),
    ("房租", ["房租", "租金", "物业费", "物业管理"]),
    ("水电费", ["电费", "水费", "燃气", "国家电网", "供电", "自来水"]),
    ("办公费", ["文具", "办公", "打印", "复印", "电脑", "鼠标", "键盘", "墨盒", "纸张"]),
]

DEFAULT_CATEGORY = "其他"


def categorize(description: str) -> str:
    """根据摘要/商户名自动判断科目。匹配不到就归「其他」。"""
    text = description or ""
    for category, keywords in CATEGORY_RULES:
        if any(kw in text for kw in keywords):
            return category
    return DEFAULT_CATEGORY


def _parse_amount(raw: str) -> float | None:
    """把 '¥30.00'、'30.00'、'-30.00' 这类字符串转成正数金额。"""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", str(raw))
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        return abs(float(cleaned))
    except ValueError:
        return None


def _read_text(path: Path) -> str:
    """读取账单文件。支付宝导出是 GBK，微信多为 UTF-8，都试一遍。"""
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return path.read_text(encoding="utf-8", errors="ignore")


def _find_header(lines: list[str]) -> int:
    """账单文件前面有一堆说明行，找到真正的表头行（含'交易时间'或'交易创建时间'）。"""
    for i, line in enumerate(lines):
        if "交易时间" in line or "交易创建时间" in line:
            return i
    return -1


def parse_bill(path: str | Path) -> list[dict]:
    """解析账单 CSV，返回交易列表。

    同时兼容支付宝和微信两种导出格式。
    """
    path = Path(path)
    text = _read_text(path)
    lines = text.splitlines()
    header_idx = _find_header(lines)
    if header_idx < 0:
        raise ValueError("没找到表头行，可能不是支付宝/微信账单文件")

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    records: list[dict] = []

    for row in reader:
        # 支付宝用「交易创建时间/商品名称/收-支」，微信用「交易时间/商品/收-支」
        date_raw = row.get("交易创建时间") or row.get("交易时间") or ""
        desc = row.get("商品名称") or row.get("商品") or row.get("交易对方") or ""
        io_type = row.get("收/支") or ""
        amount_raw = row.get("金额（元）") or row.get("金额(元)") or row.get("金额") or ""

        amount = _parse_amount(amount_raw)
        if amount is None or amount == 0:
            continue
        # 「不计收支」的（如转账、提现）跳过，只记真实的收入/支出
        if io_type and io_type not in ("支出", "收入", "收"):
            continue

        date = date_raw.strip()[:10] if date_raw.strip() else ""
        if not re.match(r"\d{4}-\d{2}-\d{2}", date):
            continue

        entry_type = "income" if ("收入" in io_type or "收" == io_type.strip()) else "expense"
        if not io_type:
            entry_type = "expense"

        records.append({
            "date": date,
            "amount": amount,
            "description": desc.strip()[:100] or "未知",
            "category": categorize(desc) if entry_type == "expense" else "营业收入",
            "entry_type": entry_type,
        })
    return records


def _fingerprint(rec: dict) -> str:
    """给一笔交易生成指纹，用于去重（同一笔不要重复导入）。"""
    key = f"{rec['date']}|{rec['amount']}|{rec['description']}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def import_bill(path: str | Path) -> dict:
    """把账单导入账本，自动去重。返回统计信息。"""
    records = parse_bill(path)

    # 查出已有指纹，避免重复导入
    with SessionLocal() as db:
        existing = {
            _fingerprint({
                "date": r.date, "amount": r.amount, "description": r.description,
            })
            for r in db.query(LedgerEntry).all()
        }

        added, skipped = 0, 0
        for rec in records:
            if _fingerprint(rec) in existing:
                skipped += 1
                continue
            # 按复式记账生成借贷分录
            if rec["entry_type"] == "expense":
                debit, credit = rec["category"], "银行存款"
            else:
                debit, credit = "银行存款", rec["category"]

            db.add(LedgerEntry(
                date=rec["date"], amount=rec["amount"], description=rec["description"],
                category=rec["category"], entry_type=rec["entry_type"],
                debit_account=debit, credit_account=credit,
            ))
            existing.add(_fingerprint(rec))
            added += 1
        db.commit()

    return {"total_parsed": len(records), "added": added, "skipped_duplicates": skipped}
