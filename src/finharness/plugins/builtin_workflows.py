"""内置 workflow：把固定流程写进代码，而不是让模型每轮自己决定。

财务工作里有大量**步骤顺序固定**的流程，最典型的是月末结账：

    对账找异常 → 独立稽核 → 确认无异常后结账

这三步的顺序在执行前就定死了，让模型自由发挥只会更慢、更不稳，
而且**有一步顺序错了后果很严重**——比如没对账就结账，等于把错误封进历史。
写成脚本后：顺序由宿主保证，模型没法跳过对账。
"""

from __future__ import annotations

from .workflow import WorkflowMeta, WorkflowRuntime

META_MONTH_END = WorkflowMeta(
    name="month_end_close",
    description="月末结账全流程：对账找异常 → 独立稽核 → 确认后结账",
    phases=["对账", "独立稽核", "结账"],
)

META_VOUCHER_REVIEW = WorkflowMeta(
    name="voucher_review",
    description="待审核单据批量复核：查准则定科目 → 给出审核建议",
    phases=["读队列", "查依据", "出建议"],
)


def month_end_close(ctx, args: dict) -> dict:
    """月末结账的标准流程。

    刻意把「对账」放在第一步：账都没对平就结账，
    等于把未发现的错误封存成历史——之后要改就得走反结账。
    """
    period = args.get("period", "")
    if not period:
        raise ValueError("workflow 需要参数 period（形如 2026-10）")

    ctx.phase("对账")
    recon = ctx.agent(
        "对账找异常",
        f"对 {period} 这个会计期间做对账。调用 reconcile_month 工具扫描："
        f"大额支出、疑似重复记账、缺凭证的单据。"
        f"输出：当期收支合计 + 异常清单（若无异常就明说）。",
        subagent="ledger_auditor",
    )

    ctx.phase("独立稽核")
    audit = ctx.agent(
        "独立稽核",
        f"对 {period} 的账目做独立稽核。背景是对账结果为：\n{recon}\n\n"
        f"请自己动手复算余额与合计，检查借贷方向、科目与摘要是否相符。"
        f"输出分三节：相符项 / 存疑项 / 错误项。",
        subagent="ledger_auditor",
    )

    ctx.phase("结账")
    close = ctx.agent(
        "执行结账",
        f"若 {period} 无存疑或错误项，调用 close_period 结账；"
        f"若有问题，**不要结账**，把问题列清楚即可。\n"
        f"稽核结论：\n{audit}",
        subagent="ledger_auditor",
    )

    return {"period": period, "reconcile": recon, "audit": audit, "close": close}


def voucher_review(ctx, args: dict) -> dict:
    """待审核单据的批量复核流程。"""
    ctx.phase("读队列")
    queue = ctx.agent(
        "读待审核队列",
        "调用 list_pending 取出所有待审核单据，列出每笔的金额、摘要、收付方向与凭证。",
        subagent="ledger_auditor",
    )

    ctx.phase("查依据")
    rules = ctx.agent(
        "查会计处理依据",
        f"针对下面这批待审核单据，查会计准则是怎么规定科目归属的：\n{queue}\n\n"
        f"每类业务给出：应记科目 + 准则依据（名称/文号/条款）。",
        subagent="rule_researcher",
    )

    ctx.phase("出建议")
    advice = ctx.agent(
        "给出审核建议",
        f"综合队列与准则依据，给出每笔单据的审核建议（通过 / 存疑 / 驳回）及理由。\n"
        f"队列：\n{queue}\n\n依据：\n{rules}",
        subagent="ledger_auditor",
    )

    return {"queue": queue, "rules": rules, "advice": advice}


def register_builtin_workflows(runtime: WorkflowRuntime) -> None:
    runtime.register(META_MONTH_END, month_end_close)
    runtime.register(META_VOUCHER_REVIEW, voucher_review)
