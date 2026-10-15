"""FastAPI + ASGI 服务层。

对应 JD 原话：「后端采用 FastAPI + ASGI 实现高性能服务」。

两套界面共用一个进程、一个端口：

    业务系统  GET  /            真实产品界面（记账/对账/报表/财务分析）
    运行时控制台 GET  /console        Agent 执行链路（实时事件流 + 任务清单 + 审批面板）

业务接口来自 `api/v1`（挂在 `/api/v1`），运行时接口是本文件定义的。

端点：
    GET  /console                   单页控制台（实时事件流 + 任务清单 + 审批面板）
    GET  /health                    健康检查
    GET  /tools                     当前工具池（含是否需要审批）
    POST /runs                      提交任务，返回 run_id
    GET  /runs                      历史任务列表
    GET  /runs/{id}                 任务详情与结论
    GET  /runs/{id}/events          SSE 实时事件流
    GET  /runs/{id}/approvals       待审批列表
    POST /runs/{id}/approvals/{aid} 提交审批决定
    GET  /traces/{trace_id}         离线链路时间线（HTML）

为什么是 SSE 而不是 WebSocket：执行事件是**单向**的（服务端 → 浏览器），
SSE 足够且能自动重连、走普通 HTTP、不引入额外协议负担。
"""

from __future__ import annotations

import json
import queue
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..api.router import api_v1
from ..assemble import build_harness
from ..config import TRACE_DIR, WEB_DIR
from ..services.chat_service import TOOL_LABELS
from ..storage.store import list_checkpoints, load_checkpoint, shared_store
from .service import RunManager

app = FastAPI(title="企业财务多智能体系统", version="0.3.0")
MANAGER = RunManager()

# 业务接口（记账/对账/报表/分析）——由 finance-agent 的业务层提供，原样挂载
app.include_router(api_v1)
# 界面依赖 echarts，走 /static
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


# —— 请求体 ——
class RunIn(BaseModel):
    goal: str = Field(..., min_length=2, description="要 Agent 做的财务任务")
    subagent: str | None = None
    # 传上一轮的 conversation_id 即为「追问」：Agent 会带着既有对话继续，
    # 而不是从零开始。不传就是一次独立任务。
    conversation_id: str | None = Field(
        default=None, description="会话 id；传了就接着这个会话追问"
    )


class HumanIn(BaseModel):
    """人工介入的回应：审批给 approved，提问给 answer，两者共用这一个端点。"""
    approved: bool = True
    answer: str = ""
    decider: str = "operator"
    note: str = ""


# —— 基础 ——
@app.get("/health")
def health() -> dict:
    return {"ok": True, "runs": len(MANAGER.runs),
            "active": sum(1 for r in MANAGER.runs.values()
                          if r.status in ("running", "awaiting_approval"))}


@app.get("/tools")
def tools() -> dict:
    """把工具池连同「是否需要审批」一起列出来——审批规则是可查的，不是黑箱。

    同时给出中文短名（`label`）：控制台的时间线要写人话，
    「月度对账」比 `reconcile_month` 好读。标签与业务界面共用同一份定义，
    免得两边各维护一套、改了不同步。
    """
    bundle = build_harness()
    out = []
    for name in bundle["registry"].names():
        t = bundle["registry"].get(name)
        out.append({
            "name": t.name, "category": t.category,
            "label": TOOL_LABELS.get(t.name, (t.name, ""))[0],
            "read_only": t.read_only, "requires_approval": t.requires_approval,
            "description": t.description.split("\n")[0][:80],
        })
    return {"count": len(out), "tools": out}


# —— 任务 ——
@app.post("/runs")
def create_run(body: RunIn) -> dict:
    rec = MANAGER.start(body.goal, conversation_id=body.conversation_id)
    return {
        "run_id": rec.run_id,
        "conversation_id": rec.conversation_id,
        "turn": rec.turn,
        "status": rec.status,
    }


@app.get("/conversations")
def list_conversations() -> dict:
    """列出所有会话。追问靠 conversation_id 接着走。"""
    items = MANAGER.list_conversations()
    return {"count": len(items), "conversations": items}


@app.get("/conversations/{cid}")
def get_conversation(cid: str) -> dict:
    c = MANAGER.conversation(cid)
    if c is None:
        raise HTTPException(404, f"未知会话 {cid}")
    return {
        "conversation_id": cid,
        "turns": c["turns"],
        "first_goal": c["first_goal"],
        "messages": len(c["messages"]),
    }


@app.get("/runs")
def list_runs() -> dict:
    items = sorted(MANAGER.runs.values(), key=lambda r: -r.created)
    return {"count": len(items), "runs": [r.to_dict() for r in items[:20]]}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    rec = MANAGER.get(run_id)
    if rec is None:
        raise HTTPException(404, f"未知任务 {run_id}")
    return rec.to_dict()


@app.get("/runs/{run_id}/approvals")
def list_approvals(run_id: str) -> dict:
    rec = MANAGER.get(run_id)
    if rec is None:
        raise HTTPException(404, f"未知任务 {run_id}")
    return {"pending": rec.pending_approvals}


@app.post("/runs/{run_id}/approvals/{approval_id}")
def decide_approval(run_id: str, approval_id: str, body: HumanIn) -> dict:
    """回应一次人工介入：审批（批准/拒绝）或提问（给答案）。"""
    ok = MANAGER.resolve_approval(run_id, approval_id, body.approved,
                                  decider=body.decider, note=body.note,
                                  answer=body.answer)
    if not ok:
        raise HTTPException(409, "该请求不存在或已处理")
    return {"ok": True, "request_id": approval_id,
            "approved": body.approved, "answer": body.answer}


# —— 断点与续跑（短期存储）——
@app.get("/checkpoints")
def checkpoints() -> dict:
    """列出所有任务快照。断点信息落的是本地 sqlite，生产环境应换 Redis。"""
    items = list_checkpoints(shared_store())
    items.sort(key=lambda c: -c.get("saved_at", 0))
    return {
        "count": len(items),
        "checkpoints": [
            {
                "run_id": c["run_id"], "goal": c.get("goal", "")[:80],
                "status": c.get("status"), "turns": c.get("turns"),
                "open_tasks": [t["id"] for t in c.get("board", [])
                               if t.get("status") not in ("completed", "failed")],
                "saved_at": c.get("saved_at"),
            }
            for c in items[:20]
        ],
    }


@app.get("/runs/{run_id}/checkpoint")
def get_checkpoint(run_id: str) -> dict:
    cp = load_checkpoint(shared_store(), run_id)
    if cp is None:
        raise HTTPException(404, f"未找到任务 {run_id} 的快照")
    return cp


@app.post("/runs/{run_id}/resume")
def resume_run(run_id: str) -> dict:
    """从断点续跑，返回新任务 id。"""
    rec = MANAGER.resume(run_id)
    if rec is None:
        raise HTTPException(404, f"未找到任务 {run_id} 的快照，无法续跑")
    return {"resumed_from": run_id, "run_id": rec.run_id, "status": rec.status}


# —— 事件流 ——
def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/runs/{run_id}/events")
def run_events(run_id: str) -> StreamingResponse:
    q = MANAGER.queue_of(run_id)
    if q is None:
        raise HTTPException(404, f"未知任务 {run_id}")

    def gen():
        yield _sse({"type": "hello", "run_id": run_id})
        while True:
            try:
                item = q.get(timeout=15)
            except queue.Empty:
                yield ": keepalive\n\n"   # 心跳，防中间层掐断空闲连接
                continue
            yield _sse(item)
            if item.get("type") in ("done", "error"):
                break

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/traces/{trace_id}")
def trace_html(trace_id: str) -> HTMLResponse:
    p: Path = TRACE_DIR / f"{trace_id}.html"
    if not p.exists():
        raise HTTPException(404, f"未找到链路 {trace_id}")
    return HTMLResponse(p.read_text(encoding="utf-8"))





@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    """真实产品界面：记账、审核、对账、报表、财务分析。"""
    return FileResponse(WEB_DIR / "index.html")


@app.get("/console", response_class=HTMLResponse)
def console() -> FileResponse:
    """运行时控制台：看 Agent 每一轮在干什么、卡在哪个审批。

    独立成 web/console.html 而不是内联在这里——内联版本没法单独看、没法调试，
    改一版要动 Python 文件。前端就该待在前端文件里。
    """
    return FileResponse(WEB_DIR / "console.html")
