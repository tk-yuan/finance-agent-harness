"""财务分析接口（多智能体）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...services.analysis import run_analysis
from ...services import finance_analysis

router = APIRouter(prefix="/analysis", tags=["analysis"])


@router.get("/metrics", summary="财务指标（纯计算，不调用大模型，秒回）")
def metrics() -> dict:
    """四个维度的指标数字。不经过 LLM，所以很快——
    页面可以先把数字显示出来，再等 AI 的解读。"""
    return finance_analysis.all_metrics()


@router.post("/report", summary="跑多智能体分析，生成财务体检报告")
def report() -> dict:
    """四个分析角色并行跑，再汇总成报告。

    注意：会调用 5 次大模型（4 个分析 + 1 个汇总），大约 30-60 秒。
    """
    try:
        return run_analysis()
    except Exception as exc:
        raise HTTPException(500, f"分析失败：{exc}") from exc
