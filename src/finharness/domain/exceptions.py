"""统一异常定义 + FastAPI 异常处理器。

业务代码抛这里的异常，由 API 层统一转成 JSON 返回，
不会把 Python 堆栈直接暴露给调用方。


就是异常处理机制
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """所有业务异常的基类。"""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


class ConfigError(AppError):
    """配置缺失或非法。"""

    status_code = 500
    code = "config_error"


def register_exception_handlers(app: FastAPI) -> None:
    """把异常统一转成结构化 JSON，而不是 FastAPI 默认的 500 页面。"""

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={"code": "internal_error", "message": "服务器内部错误，详见日志"},
        )
