"""企业档案服务。

只描述「这家公司是干什么的」，不写任何工商登记类信息。
本项目的企业与账务数据均为仿真生成，不指向任何真实企业。
"""

from __future__ import annotations

from sqlalchemy import select

from ..domain.db.models import Company
from ..domain.db.session import SessionLocal

def _build_default() -> dict:
    """构造默认档案（延迟生成，避免模块级常量被修改）。"""
    return {
        "name": "示例商贸企业",
        "industry": "批发和零售业",
        "main_business": "日用百货、办公用品的批发与零售",
        "description": (
            "一家小型商贸企业，经营模式为「南货北销」——从南方供应商采购商品，"
            "销往本地及周边的批发商与终端客户。\n\n"
            "业务涵盖商品采购、销售、员工薪酬、社保公积金、场地租金、水电、"
            "增值税、以及差旅、办公、招待等日常报销，是典型的小微企业账务形态，"
            "也是代账公司最常见的服务对象。\n\n"
            "【说明】本系统的企业与账务数据均为仿真生成，用于演示与测试，"
            "不指向任何真实企业。"
        ),
    }


DEFAULT_COMPANY = _build_default()


def get_company() -> dict | None:
    """取企业档案（没有则返回 None）。"""
    with SessionLocal() as db:
        row = db.scalars(select(Company).limit(1)).first()
    return row.as_dict() if row else None


def ensure_company() -> dict:
    """确保企业档案存在（首次启动时写入默认值）。"""
    with SessionLocal() as db:
        row = db.scalars(select(Company).limit(1)).first()
        if row is None:
            row = Company(**DEFAULT_COMPANY)
            db.add(row)
            db.commit()
            db.refresh(row)
        return row.as_dict()


def update_company(**fields) -> dict:
    """更新企业档案（只更新传入的字段）。"""
    with SessionLocal() as db:
        row = db.scalars(select(Company).limit(1)).first()
        if row is None:
            row = Company(**DEFAULT_COMPANY)
            db.add(row)
        for k, v in fields.items():
            if v is not None and hasattr(row, k):
                setattr(row, k, v)
        db.commit()
        db.refresh(row)
        return row.as_dict()
