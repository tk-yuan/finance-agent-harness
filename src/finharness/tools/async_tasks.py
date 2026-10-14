"""后台任务与定时调度的工具接口（对应教程 s11 / s12）。

## 为什么后台任务要按「名字」提交，而不是让模型写代码

模型提交的如果是可执行代码，那就是一个远程代码执行漏洞。
这里注册的是一组**预定义任务**，模型只能选名字和参数——
和 Workflow 的安全边界是同一条原则。

## 三种预定义任务

| 任务 | 干什么 | 为什么适合后台 |
|---|---|---|
| `full_audit` | 逐月对账并汇总异常 | 要跑 12 个月，几十秒起步 |
| `annual_report` | 生成全年报表 + 月度明细 | 跨期汇总，数据量大 |
| `voucher_batch_check` | 全量单据合规检查 | 逐条扫描，账目多时很慢 |

## 定时任务为什么不直接执行、而是提交后台

调度器的 tick 是**秒级**的，如果回调里直接跑几十秒的活，
调度线程就被占住，其他任务到点也触发不了。
所以：**定时器只负责触发，后台任务负责执行**——两条线在回调处对接。
"""

from __future__ import annotations

from ..runtime.tools import Tool, ToolRegistry
from ..tasks.background import BackgroundManager
from ..tasks.scheduler import Scheduler


# ═══════════════ 预定义的后台任务 ═══════════════

def _full_audit(year: str = "") -> dict:
    """逐月对账，汇总全年的异常。"""
    from datetime import datetime

    from .finance import reconcile_month

    year = year or datetime.now().strftime("%Y")
    months = [f"{year}-{m:02d}" for m in range(1, 13)]
    total, anomalies = 0, []
    for m in months:
        r = reconcile_month(m)
        if "error" in r or r.get("count", 0) == 0:
            continue
        total += r["count"]
        if r.get("anomaly_count"):
            anomalies.append({"period": m, "count": r["anomaly_count"],
                              "samples": r["anomalies"][:3]})
    return {"year": year, "audited_entries": total,
            "months_with_anomalies": len(anomalies), "anomalies": anomalies}


def _annual_report(year: str = "") -> dict:
    from datetime import datetime

    from .finance import generate_report

    year = year or datetime.now().strftime("%Y")
    rep = generate_report(year)
    return {"year": year, "report": rep,
            "note": "全年报表已生成，可写入 workspace/reports/"}


def _voucher_batch_check() -> dict:
    """全量单据合规检查：缺凭证、借贷方向异常、科目为空。"""
    from sqlalchemy import select

    from ..domain.db._session import session
    from ..domain.db.models import LedgerEntry

    with session() as db:
        rows = db.scalars(select(LedgerEntry)).all()

    problems = []
    for r in rows:
        if r.status == "approved" and not r.attachment:
            problems.append({"id": r.id, "issue": "已入账但无凭证"})
        if r.status == "approved" and not r.category:
            problems.append({"id": r.id, "issue": "已入账但科目为空"})
        if r.status == "approved" and r.debit_account == r.credit_account:
            problems.append({"id": r.id, "issue": "借贷同科目，分录异常"})
    return {"checked": len(rows), "problems": len(problems), "details": problems[:20]}


TASKS = {
    "full_audit": ("全量稽核：逐月对账并汇总异常", _full_audit),
    "annual_report": ("生成年度报表（利润表 + 资产负债表）", _annual_report),
    "voucher_batch_check": ("全量单据合规检查", _voucher_batch_check),
}


# ═══════════════ 定时任务注册 ═══════════════

def register_schedules(scheduler: Scheduler, mgr: BackgroundManager) -> None:
    """注册财务域的定时任务。

    注意每个回调都只做一件事：`mgr.submit(...)`。
    真正耗时的活在后台线程里跑，调度线程立刻返回——
    否则一个慢任务会把后面的定时任务全堵住。
    """
    scheduler.register(
        "daily_scan", "30 9 * * *",
        "每天 09:30 扫一遍异常账目（大额、重复、缺凭证）",
        lambda: (mgr.submit("daily_scan", _full_audit),
                 "已提交每日巡检到后台")[1],
    )
    scheduler.register(
        "month_end_remind", "0 9 1 * *",
        "每月 1 号提醒上个会计期间结账",
        lambda: (mgr.submit("month_end_report", _annual_report),
                 "已提交月度结账材料生成")[1],
    )
    scheduler.register(
        "weekly_voucher_check", "0 10 * * 1",
        "每周一检查单据合规性（缺凭证、科目为空等）",
        lambda: (mgr.submit("weekly_check", _voucher_batch_check),
                 "已提交单据合规检查")[1],
    )


# ═══════════════ 注册工具 ═══════════════

def register_async_tools(registry: ToolRegistry, mgr: BackgroundManager,
                         scheduler: Scheduler) -> None:
    def background_run(task: str, args: dict | None = None) -> dict:
        entry = TASKS.get(task)
        if entry is None:
            return {"error": f"未知后台任务：{task}", "available": list(TASKS)}
        _, fn = entry
        tid = mgr.submit(task, fn, **(args or {}))
        return {"ok": True, "task_id": tid, "task": task,
                "message": f"已提交到后台：{entry[0]}。跑完会自动通知，你可以先做别的。"}

    def background_status(task_id: str = "") -> dict:
        return mgr.status(task_id) if task_id else mgr.list_all()

    registry.register(Tool(
        name="background_run",
        description="把一个耗时的活提交到后台执行，不阻塞当前对话。"
                    "适用于全量稽核、年度报表、批量单据检查这类要跑几十秒的任务。",
        parameters={"type": "object", "properties": {
            "task": {"type": "string", "enum": list(TASKS),
                     "description": "任务名：" + "；".join(f"{k}={v[0]}" for k, v in TASKS.items())},
            "args": {"type": "object", "description": "参数，如 {\"year\": \"2026\"}"},
        }, "required": ["task"]},
        handler=background_run, read_only=False, category="background",
    ))
    registry.register(Tool(
        name="background_status",
        description="查后台任务的进度与结果。不传 task_id 就列出全部。",
        parameters={"type": "object", "properties": {"task_id": {"type": "string"}}},
        handler=background_status, read_only=True, category="background",
    ))
    registry.register(Tool(
        name="list_schedules",
        description="列出已注册的定时任务（cron 表达式、上次运行结果）。",
        parameters={"type": "object", "properties": {}},
        handler=scheduler.list_jobs, read_only=True, category="schedule",
    ))
    registry.register(Tool(
        name="run_schedules_now",
        description="立即检查定时任务是否到点并触发（用于演示或手动催一次，"
                    "不必真等到 cron 时间）。",
        parameters={"type": "object", "properties": {}},
        handler=lambda: {"fired": scheduler.tick(), "jobs": scheduler.list_jobs()},
        read_only=False, category="schedule",
    ))
