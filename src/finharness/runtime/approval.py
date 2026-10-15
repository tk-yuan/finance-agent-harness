"""人工介入门（human-in-the-loop）。

对应 JD 原话：「结合人工审批机制保障线上规则变更与灰度发布的安全合规。」

三条设计原则，每条都是刻意的：

1. **哪些操作要审批由规则决定，不硬编码在循环里。**
   工具可以标 `requires_approval`；也可以用规则匹配参数（比如写往受控目录）。
   审批是中间件的一环，不是循环里的 if。

2. **阻塞是刻意的** —— Agent 执行到这一步会停下来等人。
   这不是性能问题：有外部后果的动作不能「先做了再补审批」，
   否则审批只是事后通知，约束不了任何东西。

3. **超时自动拒绝**（默认 300 秒）。挂死比拒绝更糟——
   拒绝是可审计的终态，挂死是运维事故。

## 两类介入，同一套机制

    kind="approval"  工具想动有外部后果的东西（入账、招募队友）→ 要人**批准**
    kind="question"  Agent 自己判断不了、需要人给信息（"这笔算办公费还是差旅费？"）→ 要人**回答**

两者都是「停下来 → 推给前端 → 阻塞等 → 拿到结果继续」，所以共用同一个门、
同一套等待与超时逻辑，只是前端渲染的控件不同（批准/拒绝 vs 输入框）。
**不要为"提问"另造一套阻塞机制**——那会多出一份要维护的超时和线程安全。

三种工作模式共用同一个 gate，只是 notifier 不同：
  · CLI   控制台提问，人回车
  · 服务  请求经 SSE 推给前端，等 HTTP 回调 resolve()
  · 同步聊天  没有"等人"的余地，一律降级（见 chat_service）
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

DEFAULT_TIMEOUT = 300.0


@dataclass
class ApprovalRequest:
    id: str
    tool: str
    args: dict
    reason: str
    risk: str = "medium"          # low / medium / high
    kind: str = "approval"        # approval（要批准） / question（要回答）
    status: str = "pending"       # pending / approved / rejected / timeout / answered
    question: str = ""            # kind=question 时的问句
    options: list[str] = field(default_factory=list)
    answer: str = ""              # kind=question 时人给的回答
    created: float = field(default_factory=time.time)
    decided: float = 0.0
    decider: str = ""
    note: str = ""

    @property
    def waited_sec(self) -> float:
        end = self.decided or time.time()
        return round(end - self.created, 2)

    @property
    def resolved(self) -> bool:
        return self.status != "pending"


def default_rules(tool, args: dict) -> tuple[bool, str]:
    """默认审批规则。返回 (是否需要审批, 原因)。

    真实环境里这套规则由合规/风控团队维护，这里给一个可读的默认集：
      · 工具自带高风险标记
      · 写往受控目录（路径含 final / submit / archive）
      · 工具名带 submit / publish / deploy 语义
    """
    if getattr(tool, "requires_approval", False):
        return True, f"工具 {tool.name} 被标记为高风险操作"

    name = tool.name.lower()
    if any(k in name for k in ("submit", "publish", "deploy", "archive")):
        return True, f"工具 {tool.name} 有对外生效语义"

    if not getattr(tool, "read_only", True):
        target = str(args.get("path") or args.get("file") or "").lower()
        if any(k in target for k in ("final", "submit", "archive")):
            return True, f"写往受控路径：{args.get('path') or args.get('file')}"

    return False, ""


class ApprovalGate:
    def __init__(self, rules=default_rules, timeout: float = DEFAULT_TIMEOUT, notifier=None) -> None:
        self.rules = rules
        self.timeout = timeout
        self.notifier = notifier
        self._requests: dict[str, ApprovalRequest] = {}
        self._waits: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # —— 判断与发起 ——
    def requires(self, tool, args: dict) -> tuple[bool, str]:
        return self.rules(tool, args)

    def request(self, tool_name: str, args: dict, reason: str, risk: str = "medium") -> ApprovalRequest:
        """发起审批并**阻塞等待**。超时按拒绝处理。"""
        req = ApprovalRequest(id=f"ap-{uuid.uuid4().hex[:8]}", tool=tool_name,
                              args=args, reason=reason, risk=risk)
        ev = threading.Event()
        with self._lock:
            self._requests[req.id] = req
            self._waits[req.id] = ev

        if self.notifier is not None:
            try:
                self.notifier(req)
            except Exception:  # noqa: BLE001 —— 通知失败不应拖垮流程，转超时
                pass

        if not ev.wait(self.timeout):
            req.status = "timeout"
            req.decided = time.time()
            req.note = f"等待 {self.timeout:.0f} 秒未获批复，按拒绝处理"
        return req

    def ask(self, question: str, options: list[str] | None = None,
            tool_name: str = "ask_user") -> ApprovalRequest:
        """向人提问并**阻塞等待回答**。与 request() 共用同一套等待/超时逻辑。

        超时不算"拒绝"——回答不像批准那样二选一，所以超时后 answer 为空字符串，
        由调用方（工具）决定怎么降级（通常是"请自行合理假设并说明"）。
        """
        req = ApprovalRequest(id=f"q-{uuid.uuid4().hex[:8]}", tool=tool_name,
                              args={}, reason="需要用户补充信息",
                              risk="low", kind="question",
                              question=question, options=list(options or []))
        ev = threading.Event()
        with self._lock:
            self._requests[req.id] = req
            self._waits[req.id] = ev

        if self.notifier is not None:
            try:
                self.notifier(req)
            except Exception:  # noqa: BLE001 —— 通知失败不应拖垮流程，转超时
                pass

        if not ev.wait(self.timeout):
            req.status = "timeout"
            req.decided = time.time()
            req.note = f"等待 {self.timeout:.0f} 秒未获回答"
        return req

    def resolve(self, req_id: str, approved: bool = True, decider: str = "",
                note: str = "", answer: str = "") -> bool:
        """批准 / 拒绝 / 回答问题。服务模式下由 HTTP 端点调用。

        提问类请求走 answer（不是 approved）——把它和审批分开，是为了让
        「批准」这个动作永远只有二值语义，不会被一段自由文本污染。
        """
        with self._lock:
            req = self._requests.get(req_id)
            ev = self._waits.get(req_id)
        if req is None or ev is None or req.status != "pending":
            return False
        if req.kind == "question":
            req.status = "answered"
            req.answer = answer if answer != "" else note
        else:
            req.status = "approved" if approved else "rejected"
        req.decider = decider
        req.note = note
        req.decided = time.time()
        ev.set()
        return True

    # —— 查询 ——
    def pending(self) -> list[ApprovalRequest]:
        return [r for r in self._requests.values() if r.status == "pending"]

    def history(self) -> list[dict]:
        return [asdict(r) for r in self._requests.values()]

    def stats(self) -> dict:
        out: dict[str, int] = {}
        for r in self._requests.values():
            out[r.status] = out.get(r.status, 0) + 1
        return {"total": len(self._requests), "by_status": out}
