"""企业档案的数据契约。"""

from __future__ import annotations

from pydantic import BaseModel


class CompanyUpdate(BaseModel):
    """更新企业档案（字段都可选，只更新传入的）。"""

    name: str | None = None
    industry: str | None = None
    main_business: str | None = None
    description: str | None = None
