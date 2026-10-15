"""人工审批门的测试。

重点测**阻塞语义**：Agent 必须真的等人批复，超时必须自动拒绝（不能挂死）。
"""

import threading
import time

from finharness.runtime.approval import ApprovalGate, default_rules
from finharness.runtime.tools import Tool


def _tool(name, read_only=True, requires_approval=False):
    return Tool(name=name, description="d", parameters={}, handler=lambda **k: None,
                read_only=read_only, requires_approval=requires_approval)


# —— 规则 ——
def test_flagged_tool_requires_approval():
    need, reason = default_rules(_tool("post_voucher", requires_approval=True), {})
    assert need and "高风险" in reason


def test_semantic_name_requires_approval():
    """名字带 submit/publish/deploy 语义的，即使没打标记也要审批。"""
    for n in ("publish_policy", "deploy_rule", "archive_report"):
        need, _ = default_rules(_tool(n), {})
        assert need, n


def test_write_to_controlled_path_requires_approval():
    need, reason = default_rules(_tool("write_file", read_only=False),
                                 {"path": "reports/final/plan.md"})
    assert need and "受控路径" in reason


def test_ordinary_readonly_tool_needs_no_approval():
    need, _ = default_rules(_tool("search_accounting_rules"), {"query": "金融工具"})
    assert not need


def test_ordinary_write_needs_no_approval():
    need, _ = default_rules(_tool("write_file", read_only=False),
                            {"path": "reports/draft.md"})
    assert not need


# —— 阻塞语义 ——
def test_approve_unblocks_the_waiter():
    gate = ApprovalGate(rules=lambda t, a: (True, "测试"), timeout=5)
    got = {}

    def waiter():
        got["req"] = gate.request("post_voucher", {}, "测试")

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.15)
    assert gate.pending(), "发起后应处于待审批状态"
    pid = gate.pending()[0].id
    assert gate.resolve(pid, approved=True, decider="tester")
    t.join(timeout=3)
    assert got["req"].status == "approved"
    assert got["req"].decider == "tester"


def test_reject_unblocks_and_marks_rejected():
    gate = ApprovalGate(rules=lambda t, a: (True, "测试"), timeout=5)

    def waiter():
        gate.request("post_voucher", {}, "测试")

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.15)
    pid = gate.pending()[0].id
    gate.resolve(pid, approved=False, note="证据不足")
    t.join(timeout=3)
    hist = gate.history()
    assert hist[0]["status"] == "rejected"
    assert hist[0]["note"] == "证据不足"


def test_timeout_auto_rejects():
    """超时必须自动拒绝——挂死比拒绝糟得多，拒绝是可审计的终态。"""
    gate = ApprovalGate(rules=lambda t, a: (True, "测试"), timeout=0.3)
    req = gate.request("post_voucher", {}, "测试")   # 无人批复
    assert req.status == "timeout"
    assert "未获批复" in req.note


def test_resolve_unknown_request_returns_false():
    gate = ApprovalGate()
    assert gate.resolve("ap-nope", approved=True) is False


def test_double_resolve_is_ignored():
    """已处理的审批不能被二次改写——否则审计链就不可信了。"""
    gate = ApprovalGate(rules=lambda t, a: (True, "x"), timeout=5)

    def waiter():
        gate.request("t", {}, "x")

    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.15)
    pid = gate.pending()[0].id
    assert gate.resolve(pid, approved=True) is True
    assert gate.resolve(pid, approved=False) is False
    th.join(timeout=3)


def test_notifier_is_called():
    seen = []
    gate = ApprovalGate(rules=lambda t, a: (True, "x"), timeout=0.2,
                        notifier=lambda req: seen.append(req.tool))
    gate.request("post_voucher", {}, "x")
    assert seen == ["post_voucher"]


def test_notifier_failure_does_not_break_flow():
    """通知发不出去（网络/前端挂了），不能把整个审批流程带崩，转超时即可。"""

    def boom(req):
        raise RuntimeError("前端挂了")

    gate = ApprovalGate(rules=lambda t, a: (True, "x"), timeout=0.2, notifier=boom)
    req = gate.request("post_voucher", {}, "x")
    assert req.status == "timeout"


def test_stats_counts_outcomes():
    gate = ApprovalGate(rules=lambda t, a: (True, "x"), timeout=0.2)
    gate.request("t", {}, "x")            # 超时
    assert gate.stats()["by_status"].get("timeout") == 1


# ═══════════ 提问（人工介入的另一种形态）═══════════
#
# 和审批共用一套阻塞机制，但语义不同：
#   审批 = 人有否决权（批/拒）；提问 = 人供给信息（一段文本）。
# 所以批准永远保持二值，不会被自由文本污染。

def test_ask_returns_the_answer():
    gate = ApprovalGate(timeout=5)
    gate.notifier = lambda req: threading.Timer(
        0.1, lambda: gate.resolve(req.id, answer="记主营业务成本", decider="会计")
    ).start()
    req = gate.ask("这笔采购算办公费还是成本？", ["办公费", "主营业务成本"])
    assert req.status == "answered"
    assert req.answer == "记主营业务成本"
    assert req.kind == "question"


def test_ask_carries_options():
    gate = ApprovalGate(timeout=5)
    seen = {}
    gate.notifier = lambda req: (seen.update(req=req),
                                 threading.Timer(0.1, lambda: gate.resolve(req.id, answer="A")).start())
    gate.ask("选哪个？", ["A", "B"])
    assert seen["req"].options == ["A", "B"]
    assert seen["req"].question == "选哪个？"


def test_ask_timeout_is_not_a_rejection():
    """超时和"被拒绝"不是一回事：回答是文本，没有"拒绝"这个值。
    超时后 answer 为空，由调用方决定怎么降级。"""
    gate = ApprovalGate(timeout=0.2)
    req = gate.ask("问一句没人理")
    assert req.status == "timeout"
    assert req.answer == ""


def test_approved_flag_does_not_replace_answer():
    """提问走 answer，不走 approved——两者分开，语义才不会被搅在一起。"""
    gate = ApprovalGate(timeout=5)
    gate.notifier = lambda req: threading.Timer(
        0.1, lambda: gate.resolve(req.id, approved=False, answer="照旧记办公费")
    ).start()
    req = gate.ask("这笔记哪？")
    assert req.status == "answered"
    assert req.answer == "照旧记办公费"


# ═══════════ ask_user 工具：三种环境的降级 ═══════════

def _ask_tool(gate):
    from finharness.tools.human import register_human_tools
    from finharness.runtime.tools import ToolRegistry
    reg = ToolRegistry()
    register_human_tools(reg, gate)
    return reg.get("ask_user").handler


def test_ask_user_tool_returns_answer():
    gate = ApprovalGate(timeout=5)
    gate.notifier = lambda req: threading.Timer(
        0.1, lambda: gate.resolve(req.id, answer="是重复报销")
    ).start()
    r = _ask_tool(gate)("这张发票是不是重复报销？")
    assert r["ok"] and r["answer"] == "是重复报销"


def test_ask_user_tool_without_gate_degrades():
    """没有门（CLI / 单测装配）时不是报错，而是让模型自己合理假设。
    注册不依赖 gate——否则线上和测试的工具池形状就不一样了。"""
    r = _ask_tool(None)("这个记哪？")
    assert "error" in r and "合理假设" in r["error"]


def test_ask_user_tool_on_timeout_degrades():
    r = _ask_tool(ApprovalGate(timeout=0.2))("没人回答")
    assert "error" in r and "假设" in r["error"]


def test_ask_user_is_registered_even_without_gate():
    from finharness.assemble import build_harness
    names = build_harness()["registry"].names()
    assert "ask_user" in names


def test_sync_chat_gate_degrades_gracefully():
    """业务界面是同步请求，HTTP 连接不能挂着等人打字。
    所以那边一律返回"没问到"，工具降级——而不是把请求挂死。"""
    from finharness.services.chat_service import _RejectingGate
    req = _RejectingGate().ask("这个记哪？")
    assert req.status == "timeout"
    assert req.answer == ""
