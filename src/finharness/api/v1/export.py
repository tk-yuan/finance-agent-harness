"""报表导出接口。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import Response

from ...services import export_service

router = APIRouter(prefix="/export", tags=["export"])


@router.get("/report", summary="导出 Excel 财务报表（明细 + 余额 + 概况）")
def export_report() -> Response:
    content = export_service.build_workbook()
    filename = export_service.report_filename()

    # HTTP 响应头只能用 latin-1，中文文件名直接放进去会报 UnicodeEncodeError。
    # 按 RFC 5987 用 filename*=UTF-8''<url编码> 传中文名，
    # 同时给一个 ASCII 兜底名，保证所有浏览器都能下载。
    ascii_fallback = "financial_report.xlsx"
    disposition = (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )
