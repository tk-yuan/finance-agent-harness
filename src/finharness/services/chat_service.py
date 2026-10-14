"""业务服务层：把界面的一次对话变成一次 Agent 调用。

## 相对原版的改动

原来这里 `build_agent()` 拿的是 LangGraph 编出来的图，`invoke({"messages": ...})` 走一次。
现在换成自研运行时：**架构换了，业务层一行没改**——这正是分层的价值。

## 一个必须想清楚的点：网页里的审批怎么办

新运行时有两个高风险工具走人工审批：`post_voucher`（入账）、`spawn_teammate`（招募队友）。

但在网页**同步聊天**里，HTTP 请求不能挂在那儿等几十秒等人点按钮。
所以这里的策略是：**拒绝，并把话说明白**——

    Agent 想入账 → 被拦 → 回复「这笔需要会计审核，请到 📚 智能会计 页面操作」

这不是妥协，**是内控本身的要求**：
AI 不能替会计签字，聊天框里也不该有签字的口子。
真正的在线审批走独立的异步接口（见 `server/service.py` 的 SSE + 审批卡片）。
"""

from __future__ import annotations

from ..assemble import build_harness
from ..domain.exceptions import AppError
from ..observability.trace import Trace
from ..schemas.chat import ChatRequest, ChatResponse, ToolCall
from ..services import conversation_service

# 工具名 → (友好标签, 动作类型)。前端靠这个区分「记账」和「查询」。
TOOL_LABELS: dict[str, tuple[str, str]] = {
    "submit_voucher": ("登记单据", "record"),
    "withdraw_voucher": ("撤回单据", "delete"),
    "post_voucher": ("审核入账", "approve"),
    "reject_voucher": ("驳回单据", "approve"),
    "recategorize": ("改科目", "approve"),
    "query_entries": ("查账", "query"),
    "get_account_balance": ("查余额", "query"),
    "query_counterparty": ("查往来", "query"),
    "reconcile_month": ("对账", "reconcile"),
    "close_period": ("月末结账", "reconcile"),
    "generate_report": ("出报表", "query"),
    "search_accounting_rules": ("查准则", "query"),
    "get_current_time": ("查日期", "query"),
    "days_between": ("算天数", "query"),
    "task": ("委派子任务", "delegate"),
    "spawn_teammate": ("招募队友", "team"),
    "assign_task": ("派活", "team"),
    "team_board": ("看团队", "team"),
    "background_run": ("提交后台任务", "background"),
    "background_status": ("查后台", "background"),
    "run_workflow": ("跑固定流程", "workflow"),
    "write_file": ("写文件", "fs"),
    "read_file": ("读文件", "fs"),
}

# 需要人工审批、因此在同步聊天里会被拒绝的工具
NEEDS_APPROVAL_HINT = {
    "post_voucher": "审核入账需要会计在「📚 智能会计」页面操作——AI 不能替会计签字。",
    "spawn_teammate": "招募队友需要先确认分工方案——请在界面上确认后再启动。",
}


class _RejectingGate:
    """同步聊天里用的审批门：任何需审批的动作直接拒绝。

    这样内控不会被绕过，同时 HTTP 请求不会挂住等人点按钮。
    """

    timeout = 0.0

    def __init__(self) -> None:
        self.denied: list[str] = []

    def requires(self, tool, args):
        from ..runtime.approval import default_rules
        return default_rules(tool, args)

    def request(self, tool_name: str, args, reason: str, risk: str = "medium"):
        from ..runtime.approval import ApprovalRequest
        self.denied.append(tool_name)
        req = ApprovalRequest(id="sync-denied", tool=tool_name, args=args,
                              reason=reason, risk=risk)
        req.status = "rejected"
        req.note = NEEDS_APPROVAL_HINT.get(
            tool_name, "该操作需要人工确认，请在界面上完成。")
        return req

    def history(self):
        return []


class ChatService:
    """对话编排：一次用户请求 → 一次 Agent 运行（带会话历史）。"""

    def __init__(self, harness: dict | None = None) -> None:
        self._harness = harness
        self._gate = _RejectingGate()

    @property
    def harness(self) -> dict:
        if self._harness is None:
            self._harness = build_harness(approval_gate=self._gate)
        return self._harness

    def chat(self, request: ChatRequest | None = None, *,
             message: str = "", session_id: str = "default",
             voucher_name: str = "") -> ChatResponse:
        conv_id = session_id or (request.session_id if request else "default")
        user_message = message or (request.message if request else "")

        conversation_service.ensure_conversation(conv_id)
        history = conversation_service.load_history(conv_id)
        conversation_service.add_message(conv_id, "user", user_message, auto_title=True)

        content = user_message
        if voucher_name:
            content = (
                f"{user_message}\n\n"
                f"〔系统信息：用户本次上传了凭证，文件名为 {voucher_name}。"
                f"如果用户要求记账，调用 submit_voucher 时 voucher 参数填这个文件名。〕"
            )

        harness = self.harness
        agent = harness["agent"]
        # harness 是缓存的（记忆、任务板要跨轮保留），所以 trace 不能跟着缓存——
        # 每次对话换一份，否则界面上会显示出上一轮跑过的工具。
        trace = Trace(goal=content)
        prev_trace = agent.trace
        agent.trace = trace

        try:
            result = agent.run(content, history=history)
        except Exception as exc:  # noqa: BLE001
            raise AppError(f"模型调用失败：{exc}") from exc
        finally:
            agent.trace = prev_trace

        answer = result.answer or "（没有产出结论）"
        tool_calls = _extract_tool_calls(trace)

        # 被审批门拦下的，在回答末尾补一句指引——用户得知道下一步去哪
        if self._gate.denied:
            hints = []
            for n in dict.fromkeys(self._gate.denied):
                hints.append(NEEDS_APPROVAL_HINT.get(n, f"{n} 需要人工确认"))
            answer += "\n\n（" + "；".join(hints) + "）"
            self._gate.denied.clear()

        conversation_service.add_message(conv_id, "assistant", answer)

        kinds = [TOOL_LABELS.get(tc.name, ("", "chat"))[1] for tc in tool_calls]
        for k in ("approve", "record", "reconcile", "team", "delegate", "query"):
            if k in kinds:
                return ChatResponse(session_id=conv_id, answer=answer,
                                    tool_calls=tool_calls, kind=k)

        return ChatResponse(session_id=conv_id, answer=answer,
                            tool_calls=tool_calls, kind="chat")


def _extract_tool_calls(trace: Trace) -> list[ToolCall]:
    """从执行链路里取出这次跑了哪些工具，转成前端能显示的标签。

    比原版从 LangGraph 的 messages 里翻更直接——**trace 本来就是为这个存在的**：
    它是运行时的标准产出，不依赖任何框架的内部结构。
    """
    calls: list[ToolCall] = []
    seen: set[str] = set()
    for span in trace.spans:
        if span["kind"] != "tool":
            continue
        name = span["name"]
        label, _ = TOOL_LABELS.get(name, (name, "chat"))
        key = f"{name}:{span['fields'].get('args', '')}"
        if key in seen:
            continue
        seen.add(key)
        calls.append(ToolCall(
            name=name, label=label,
            arg=str(span["fields"].get("args", ""))[:60],
        ))
    return calls
