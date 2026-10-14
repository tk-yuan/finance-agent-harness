"""定时调度（对应教程 s12）：到点自己干活。

## 财务域为什么真的需要它

不像有些域是"为了用上而用上"——财务工作**天然按日历走**：

  · 每天开盘前扫一遍异常账目
  · 每月 1 号提醒上个期间该结账了
  · 每周一检查有没有逾期未收的应收款
  · 准则更新后重新索引知识库

这些活没有"用户来问"这个触发点，**只能靠时间触发**。

## 设计要点

**① cron 表达式自己解析，不引第三方库。**
标准 5 段式（分 时 日 月 周），支持 `*`、`*/n`、`a,b`、`a-b`。
够用，且少一个依赖——这是作品集项目，克隆下来能跑比功能多更重要。

**② 到点匹配 + 幂等。** 同一个分钟内只触发一次，靠 `last_run` 的分钟粒度去重。
否则定时器每秒 tick 一次，同一个任务会连跑 60 遍。

**③ 只标记、不执行重活。** 调度器本身只负责"到点了"，
真正的干活交给回调（可以是提交一个后台任务）。
两条线（s11 后台任务 + s12 定时调度）在这里对接——
**定时器负责触发，后台任务负责执行**，互不阻塞。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

# cron 字段取值范围：分 时 日 月 周
_FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
_NAMES = ("分", "时", "日", "月", "周")


def _parse_field(expr: str, lo: int, hi: int) -> set[int]:
    """解析单个 cron 字段 → 可匹配的取值集合。

    支持：*  、  */n  、  a,b,c  、  a-b  、  a-b/n
    """
    out: set[int] = set()
    for part in expr.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = max(1, int(s))
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        out.update(range(start, end + 1, step))
    return out


def parse_cron(expr: str) -> list[set[int]]:
    """解析 5 段式 cron 表达式，返回每段的可匹配集合。"""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron 表达式要 5 段（分 时 日 月 周），收到 {len(parts)} 段：{expr}")
    return [_parse_field(p, lo, hi) for p, (lo, hi) in zip(parts, _FIELDS)]


def matches(expr: str, when: time.struct_time) -> bool:
    vals = (when.tm_min, when.tm_hour, when.tm_mday, when.tm_mon,
            (when.tm_wday + 1) % 7)      # Python 周一=0，cron 周日=0
    sets = parse_cron(expr)
    return all(v in s for v, s in zip(vals, sets))


@dataclass
class CronJob:
    name: str
    cron: str
    description: str
    handler: Callable[[], object]
    enabled: bool = True
    last_run_minute: str = ""
    last_result: str = ""
    run_count: int = 0
    spec: list[set[int]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.spec = parse_cron(self.cron)

    def _match(self, when: time.struct_time) -> bool:
        vals = (when.tm_min, when.tm_hour, when.tm_mday, when.tm_mon,
                (when.tm_wday + 1) % 7)
        return all(v in s for v, s in zip(vals, self.spec))

    def minute_key(self, when: time.struct_time) -> str:
        return time.strftime("%Y-%m-%d %H:%M", when)

    def describe(self) -> dict:
        return {"name": self.name, "cron": self.cron, "description": self.description,
                "enabled": self.enabled, "run_count": self.run_count,
                "last_result": self.last_result[:120]}


class Scheduler:
    def __init__(self, tick_seconds: int = 20) -> None:
        self.jobs: dict[str, CronJob] = {}
        self.tick_seconds = tick_seconds
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.fired: list[dict] = []      # 触发记录，供主循环注入上下文

    def register(self, name: str, cron: str, description: str,
                 handler: Callable[[], object], enabled: bool = True) -> CronJob:
        job = CronJob(name=name, cron=cron, description=description,
                      handler=handler, enabled=enabled)
        self.jobs[name] = job
        return job

    # —— 到点检查 ——
    def tick(self, now: float | None = None) -> list[dict]:
        """检查所有任务，触发到点的。同一分钟内只触发一次（幂等）。"""
        when = time.localtime(now or time.time())
        key = time.strftime("%Y-%m-%d %H:%M", when)
        fired: list[dict] = []

        for job in self.jobs.values():
            if not job.enabled or job.last_run_minute == key:
                continue
            if not job._match(when):
                continue
            job.last_run_minute = key
            job.run_count += 1
            try:
                result = job.handler()
                job.last_result = str(result)[:200]
                status = "ok"
            except Exception as e:  # noqa: BLE001 —— 定时任务失败不能拖垮调度器
                job.last_result = f"{type(e).__name__}: {e}"
                status = "error"
            rec = {"name": job.name, "cron": job.cron, "status": status,
                   "result": job.last_result, "at": key}
            fired.append(rec)
            self.fired.append(rec)
        return fired

    def drain_fired(self) -> list[dict]:
        """取走触发记录，供 before_model Hook 注入上下文。"""
        out, self.fired = self.fired, []
        return out

    # —— 常驻线程（服务模式下用）——
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.wait(self.tick_seconds):
                self.tick()

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def list_jobs(self) -> dict:
        return {"count": len(self.jobs),
                "jobs": [j.describe() for j in self.jobs.values()]}


_scheduler: Scheduler | None = None


def shared_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler
