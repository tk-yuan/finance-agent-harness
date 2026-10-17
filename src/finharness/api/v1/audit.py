"""全面稽核接口：点按钮直接招常驻稽核员，不经过模型判断。

和聊天里的委派不同，这里是**产品功能**：
用户明确要求"全面稽核"，代码就直接招募只读的稽核员并派活，
不需要模型再判断一次该不该派。

稽核员在独立线程里跑，接口立刻返回 job_id，前端轮询进度。
"""

from __future__ import annotations

import threading
import time
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...assemble import build_harness
from ...domain.db.models import LedgerEntry
from ...domain.db.session import SessionLocal
from ...tools.team_tools import default_teammate_specs
from sqlalchemy import select

router = APIRouter(prefix="/audit", tags=["audit"])

AUDIT_TIMEOUT_SEC = 420
EXTRACT_CAP = 120


def _extract_entries(period: str) -> tuple[str, int]:
    """把本期的已入账账目导成紧凑清单，随任务交给稽核员。

    不导的话，稽核员只能靠 query_entries 一次看 10 条，翻十几页就撞上下文预算。
    """
    with SessionLocal() as db:
        rows = db.scalars(
            select(LedgerEntry).where(
                LedgerEntry.status == "approved",
                LedgerEntry.date >= f"{period}-01",
                LedgerEntry.date <= f"{period}-31",
            ).order_by(LedgerEntry.date, LedgerEntry.id)
        ).all()

    lines = ["id|日期|金额|类型|科目|借→贷|附件|摘要"]
    for r in rows[:EXTRACT_CAP]:
        lines.append(
            f"{r.id}|{r.date}|{r.amount:.2f}|{r.entry_type}|{r.category}|"
            f"{r.debit_account}→{r.credit_account}|{r.attachment or '无'}|{r.description}"
        )
    if len(rows) > EXTRACT_CAP:
        lines.append(f"…（共 {len(rows)} 条，这里只列前 {EXTRACT_CAP} 条）")
    return "\n".join(lines), len(rows)

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


class AuditIn(BaseModel):
    period: str


def _update(job_id: str, **fields) -> None:
    with _lock:
        _jobs[job_id].update(fields)


def _run_audit(job_id: str, period: str) -> None:
    team = None
    try:
        h = build_harness()
        team = h["team"]
        spec = default_teammate_specs()["auditor_wang"]
        spawned = team.spawn(spec)
        if not spawned.get("ok"):
            raise RuntimeError(spawned.get("error", "招募失败"))

        subject = f"全面稽核 {period} 期间全部账目"
        extract, total = _extract_entries(period)
        assigned = team.assign(
            "auditor_wang", subject,
            f"对 {period} 期间的全部账目做一次全面稽核（只读，不要修改任何账目）。\n"
            f"代码已从账套导出本期 {total} 条已入账账目，清单如下：\n\n"
            f"{extract}\n\n"
            "要求：\n"
            "1. 基于这份清单核对：金额、借贷方向、科目与摘要、凭证是否齐全、有无重复记账。\n"
            "2. 清单已含全部账目，不要反复调用 query_entries 翻页；需要看凭证或余额时再用工具。\n"
            "3. 只核对账目本身，不要检索会计准则；除非发现明确的科目错误，最多查一次。\n"
            "4. 直接输出三节：相符项 / 存疑项 / 错误项；"
            "存疑和错误项写清理由与单据号；没发现就写「未发现异常」。\n"
            "5. 结论控制在 1200 字以内。",
        )
        if not assigned.get("ok"):
            raise RuntimeError(assigned.get("error", "派活失败"))

        task_id = assigned["task_id"]
        _update(job_id, status="running", task_id=task_id, teammate="auditor_wang")

        deadline = time.monotonic() + AUDIT_TIMEOUT_SEC
        result = ""
        while time.monotonic() < deadline:
            for m in team.bus.read_inbox("lead"):
                if m.get("metadata", {}).get("task_id") == task_id:
                    result = m.get("content", "")
                    break
            if result:
                break
            time.sleep(1.0)

        if not result:
            raise TimeoutError(f"稽核员 {AUDIT_TIMEOUT_SEC} 秒内没有回报")
        _update(job_id, status="done", result=result)
    except Exception as e:  # noqa: BLE001 —— 后台任务失败要落成状态给前端看
        _update(job_id, status="failed", error=f"{type(e).__name__}: {e}")
    finally:
        if team is not None:
            try:
                team.shutdown("auditor_wang")
            except Exception:  # noqa: BLE001
                pass


@router.post("/team", summary="招常驻稽核员做全面稽核（异步）")
def start_team_audit(body: AuditIn) -> dict:
    period = body.period.strip()
    if not period:
        raise HTTPException(400, "请指定稽核期间")

    job_id = f"au-{uuid.uuid4().hex[:8]}"
    with _lock:
        _jobs[job_id] = {"job_id": job_id, "period": period, "status": "starting",
                         "started": time.time(), "result": "", "error": ""}
    threading.Thread(target=_run_audit, args=(job_id, period), daemon=True).start()
    return {"ok": True, "job_id": job_id, "period": period}


@router.get("/team/{job_id}", summary="查全面稽核进度与结论")
def audit_status(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"未知稽核任务 {job_id}")
        out = dict(job)
    out["elapsed_sec"] = round(time.time() - out["started"], 1)
    return out
