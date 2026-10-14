"""后台任务（对应教程 s11）：长任务不阻塞主对话。

## 什么时候需要它

财务工作里有不少**跑起来要几十秒到几分钟**的活：

  · 把一整批发票导入并逐张解析
  · 对几百条账目做全量稽核
  · 生成年度报表（要跨 12 个期间汇总）

这些如果同步跑，用户就得干等——而且 Agent 的对话上下文会被"正在跑"这件事占着。
后台任务让主 Agent 扔出去就继续干别的，跑完再回来收结果。

## 两条设计原则

**① 结果不直接塞进上下文，而是排队等主循环来取。**
后台任务可能跑几分钟，这期间对话早就往前走了。
直接把结果塞进去会打断当前思路。所以放进 `_pending` 队列，
由 `before_model` Hook 在下一次模型调用前注入——**时机由运行时决定，不由任务决定**。

**② 失败要留痕，不能静默消失。**
后台任务最怕"提交了但一直没结果，也不知道是还在跑还是挂了"。
所以状态机是 `running → completed / failed`，失败带完整错误，
且永远可查（不像有些实现跑完就从内存里删了）。
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

# 同时运行的后台任务上限。财务场景是单机，不需要太高，
# 太高反而会打爆数据库连接。
MAX_WORKERS = 4


@dataclass
class BackgroundTask:
    task_id: str
    name: str
    status: str = "running"        # running / completed / failed
    created: float = field(default_factory=time.time)
    finished: float = 0.0
    result: Any = None
    error: str = ""

    @property
    def elapsed(self) -> float:
        return round((self.finished or time.time()) - self.created, 1)

    def summary(self) -> dict:
        d = {"task_id": self.task_id, "name": self.name, "status": self.status,
             "elapsed_sec": self.elapsed}
        if self.status == "completed":
            d["result"] = self.result
        elif self.status == "failed":
            d["error"] = self.error
        return d


class BackgroundManager:
    """后台任务管理器。线程池跑任务，结果进队列等主循环来取。"""

    def __init__(self, max_workers: int = MAX_WORKERS) -> None:
        self.tasks: dict[str, BackgroundTask] = {}
        self._pending: list[str] = []          # 已完成但还没被取走的
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(max_workers)

    def submit(self, name: str, fn: Callable[..., Any], *args, **kwargs) -> str:
        tid = f"bg-{uuid.uuid4().hex[:8]}"
        task = BackgroundTask(task_id=tid, name=name)
        with self._lock:
            self.tasks[tid] = task

        def _run() -> None:
            with self._sem:
                t0 = time.time()
                try:
                    task.result = fn(*args, **kwargs)
                    task.status = "completed"
                except Exception as e:  # noqa: BLE001 —— 后台任务的异常不能炸主进程
                    task.status = "failed"
                    task.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-500:]}"
                task.finished = t0 + (time.time() - t0)
            with self._lock:
                self._pending.append(tid)

        threading.Thread(target=_run, daemon=True).start()
        return tid

    # —— 查询 ——
    def status(self, task_id: str) -> dict:
        t = self.tasks.get(task_id)
        return t.summary() if t else {"error": f"未知任务 {task_id}"}

    def list_all(self) -> dict:
        return {
            "count": len(self.tasks),
            "running": [t.summary() for t in self.tasks.values() if t.status == "running"],
            "recent_done": [t.summary() for t in
                            sorted(self.tasks.values(), key=lambda x: -x.finished)
                            if t.status != "running"][:5],
        }

    # —— 供 Hook 调用：取走已完成的通知 ——
    def drain_notifications(self) -> list[dict]:
        """取走所有「已完成但还没通知过」的任务结果。

        由 `before_model` Hook 在每轮模型调用前调用——
        **注入时机由运行时决定，不由后台任务决定**，这样才不会打断正在进行的推理。
        """
        with self._lock:
            ids, self._pending = self._pending, []
        return [self.tasks[i].summary() for i in ids]


_manager: BackgroundManager | None = None


def shared_manager() -> BackgroundManager:
    global _manager
    if _manager is None:
        _manager = BackgroundManager()
    return _manager
