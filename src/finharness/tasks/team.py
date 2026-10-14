"""Agent Teams（对应教程 s13）：持久队友、消息总线、共享任务板。

## 和「子 Agent 委派」（tasks/subagent.py）的区别

这两件事经常被混为一谈，但机制完全不同：

| | 子 Agent（s06） | 队友（s13） |
|---|---|---|
| 生命周期 | 一次调用后结束 | `WORK → IDLE → WORK`，直到关机 |
| 上下文 | 只服务一个任务 | **跨任务保留**（记得自己干过什么） |
| 通信 | 返回一次结论 | **双向**：收消息、发事件 |
| 派活方式 | 主 Agent 逐个委派 | 空闲队友**自己认领** ready task |
| 数量 | 用完即弃 | 常驻，有身份 |

一句话：**子 Agent 是"外包一个活"，队友是"雇了个会自己找活干的员工"。**

## 财务域为什么特别适合这个模型

财务工作本来就是**一个团队在做**——出纳、会计、稽核各司其职，
而且真实团队里也不是主管一条条派活，是**谁空了谁接**。

## 四条设计要点（照抄 s13，但用财务角色）
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import CACHE_DIR
from ..runtime.loop import Agent, RunState
from ..runtime.tools import Tool, ToolRegistry

MAILBOX_DIR = CACHE_DIR / "mailboxes"

# 运行时保留的身份名，队友不能占用
RESERVED = {"lead", "agent"}


# ═══════════════ 消息总线 ═══════════════

class MessageBus:
    """每个 Agent 一个 `.mailboxes/<name>.jsonl` 收件箱。

    **为什么不共享 messages 数组**：一个队友的工具结果一旦进入另一个队友的
    推理上下文，两边就串味了——稽核员会看到出纳的原始思考过程，
    它就很难保持独立判断。收件箱把通信放在模型上下文之外。
    """

    def __init__(self, base_dir: Path = MAILBOX_DIR) -> None:
        self.base_dir = Path(base_dir)
        self._changed = threading.Condition()
        self._lock = threading.Lock()

    def _path(self, name: str) -> Path:
        return self.base_dir / f"{name}.jsonl"

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict | None = None) -> dict:
        msg = {"id": f"m{uuid.uuid4().hex[:8]}", "from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "metadata": metadata or {}, "ts": time.strftime("%H:%M:%S")}
        with self._changed:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            with self._path(to_agent).open("a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self._changed.notify_all()
        return msg

    def _read_unlocked(self, name: str) -> list[dict]:
        p = self._path(name)
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        p.unlink(missing_ok=True)      # 读走即删——只有一个消费者
        return out

    def peek(self, name: str) -> bool:
        return self._path(name).exists()

    def read_inbox(self, name: str) -> list[dict]:
        """读取并**清空**收件箱。只允许一个消费者调用。"""
        with self._changed:
            return self._read_unlocked(name)

    def wait_for_messages(self, name: str, timeout: float | None = None) -> list[dict]:
        """阻塞等待消息，用于队友的 IDLE 状态。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while not self.peek(name):
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return []
                self._changed.wait(remaining)
            return self._read_unlocked(name)


# ═══════════════ 共享任务板 ═══════════════

class SharedBoard:
    """带认领锁的任务板。

    和 `tasks/todo.py` 的 TaskBoard 的差别：那个是主 Agent 私有的，
    这个是**多个队友并发抢活**用的，所以认领必须在锁内完成——
    否则两个队友会同时认领同一个任务，干重复的活。
    """

    def __init__(self) -> None:
        self._items: list[dict] = []
        self._lock = threading.Lock()
        self._seq = 0

    def create(self, subject: str, owner: str = "", payload: dict | None = None) -> str:
        with self._lock:
            self._seq += 1
            tid = f"G{self._seq}"
            self._items.append({"id": tid, "subject": subject, "status": "pending",
                                "owner": owner, "payload": payload or {}, "result": ""})
            return tid

    def claim(self, tid: str, who: str) -> dict:
        """认领任务。**必须在锁内判断 + 写入**，否则会重复认领。"""
        with self._lock:
            for it in self._items:
                if it["id"] == tid and it["status"] == "pending":
                    it["status"] = "claimed"
                    it["owner"] = who
                    return {"ok": True, "task": dict(it)}
            return {"error": f"任务 {tid} 不存在或已被认领"}

    def claim_any(self, who: str) -> dict:
        """认领任意一个可做的任务（队友空闲时自己找活干）。

        这就是"团队"和"派活"的分水岭：**不需要主管点名**。
        """
        with self._lock:
            for it in self._items:
                if it["status"] == "pending":
                    it["status"] = "claimed"
                    it["owner"] = who
                    return {"ok": True, "task": dict(it)}
        return {"ok": False, "message": "当前没有可认领的任务"}

    def finish(self, tid: str, result: str, status: str = "done") -> dict:
        with self._lock:
            for it in self._items:
                if it["id"] == tid:
                    it["status"], it["result"] = status, result[:500]
                    return {"ok": True}
        return {"error": f"未知任务 {tid}"}

    def list_all(self) -> dict:
        with self._lock:
            items = [dict(x) for x in self._items]
        return {"count": len(items), "pending": [x for x in items if x["status"] == "pending"],
                "claimed": [x for x in items if x["status"] == "claimed"],
                "done": [x for x in items if x["status"] == "done"]}

    def render(self) -> str:
        with self._lock:
            if not self._items:
                return "（共享任务板为空）"
            mark = {"pending": " ", "claimed": "~", "done": "x", "failed": "!"}
            return "\n".join(
                f"- [{mark.get(x['status'], '?')}] {x['id']} {x['subject']}"
                f"{'  @' + x['owner'] if x['owner'] else ''}"
                for x in self._items
            )


# ═══════════════ 队友运行时 ═══════════════

@dataclass
class TeammateSpec:
    name: str
    role: str
    system_prompt: str
    allow: set[str] | None = None


@dataclass
class TeammateRuntime:
    """一个持久队友：独立上下文，WORK / IDLE 循环。"""

    spec: TeammateSpec
    agent: Agent
    bus: MessageBus
    board: SharedBoard
    lead: str = "lead"
    status: str = "idle"                     # idle / working / stopped
    current_task: str = ""
    done_count: int = 0
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    # —— 生命周期 ——
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"teammate-{self.spec.name}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.status = "stopped"
        self.bus.send(self.spec.name, self.lead, "已关机", msg_type="shutdown")

    def _loop(self) -> None:
        """WORK / IDLE 循环。

        IDLE 时**不是空转**——它在等收件箱，而且收到唤醒后会尝试认领任务。
        这就是"会自己找活干的员工"。
        """
        while not self._stop.is_set():
            msgs = self.bus.wait_for_messages(self.spec.name, timeout=1.0)

            # 收消息：可能是 Lead 的指令，也可能是系统的关机信号
            for m in msgs:
                if m["type"] == "shutdown":
                    self.status = "stopped"
                    return
                if m["type"] == "task":
                    self._work(m["metadata"].get("task_id", ""), m["content"])
                    continue

            # 空闲：自己看看有没有没人接的活
            got = self.board.claim_any(self.spec.name)
            if got.get("ok"):
                t = got["task"]
                self._work(t["id"], t["subject"])

            self.status = "idle"
            time.sleep(0.3)

    def _work(self, tid: str, prompt: str) -> None:
        self.status = "working"
        self.current_task = tid
        try:
            result = self.agent.run(prompt)
            answer = result.answer or "（无输出）"
        except Exception as e:  # noqa: BLE001 —— 队友崩了不能拖垮团队
            answer = f"[队友执行失败] {type(e).__name__}: {e}"

        if tid:
            self.board.finish(tid, answer)
        self.done_count += 1
        self.status = "idle"
        self.current_task = ""
        # 结果回报给 Lead，由运行时投递进主对话
        self.bus.send(self.spec.name, self.lead, answer,
                      msg_type="task_result", metadata={"task_id": tid})

    def summary(self) -> dict:
        return {"name": self.spec.name, "role": self.spec.role, "status": self.status,
                "current_task": self.current_task, "done": self.done_count}


# ═══════════════ 团队 ═══════════════

class Team:
    """Lead 管理的团队：招募、派活、收结果、关机。"""

    def __init__(self, lead_agent: Agent, bus: MessageBus | None = None,
                 board: SharedBoard | None = None) -> None:
        self.lead_agent = lead_agent
        self.bus = bus or MessageBus()
        self.board = board or SharedBoard()
        self.members: dict[str, TeammateRuntime] = {}
        self.events: list[dict] = []          # 待注入 Lead 上下文的团队事件
        self._lock = threading.Lock()

    # —— 招募 ——
    def spawn(self, spec: TeammateSpec) -> dict:
        if spec.name in RESERVED:
            return {"error": f"「{spec.name}」是运行时保留名，换个名字"}
        with self._lock:
            if spec.name in self.members:
                return {"error": f"队友「{spec.name}」已经在了"}

        sub = self.lead_agent.clone(
            system_prompt=spec.system_prompt, allow=spec.allow,
            state=RunState(goal=f"我是{spec.role}", depth=1),
            name=spec.name,
        )
        t = TeammateRuntime(spec=spec, agent=sub, bus=self.bus, board=self.board)
        with self._lock:
            self.members[spec.name] = t
        t.start()
        self.events.append({"type": "teammate_spawned", "name": spec.name,
                            "role": spec.role})
        return {"ok": True, "teammate": spec.name, "role": spec.role,
                "message": f"已招募队友「{spec.name}」（{spec.role}），它会自己认领任务"}

    # —— 派活 ——
    def assign(self, name: str, subject: str, prompt: str = "") -> dict:
        t = self.members.get(name)
        if t is None:
            return {"error": f"没有队友「{name}」", "members": list(self.members)}
        tid = self.board.create(subject, owner=name)
        self.bus.send("lead", name, prompt or subject, msg_type="task",
                      metadata={"task_id": tid})
        return {"ok": True, "task_id": tid, "assigned_to": name}

    def shutdown(self, name: str) -> dict:
        t = self.members.get(name)
        if t is None:
            return {"error": f"没有队友「{name}」"}
        t.stop()
        return {"ok": True, "stopped": name}

    # —— 收结果（供运行时投递）——
    def consume_results(self) -> list[dict]:
        """读取 Lead 收件箱。只允许一个消费者调用（读走即删）。"""
        msgs = self.bus.read_inbox("lead")
        for m in msgs:
            self.events.append({"type": "teammate_message", "from": m["from"],
                                "content": m["content"][:600], "msg_type": m["type"]})
        return msgs

    def drain_events(self) -> list[dict]:
        """取走团队事件，供 before_model Hook 注入上下文。"""
        with self._lock:
            out, self.events = self.events, []
        return out

    def list_members(self) -> dict:
        return {"count": len(self.members),
                "members": [t.summary() for t in self.members.values()],
                "board": self.board.list_all()}
