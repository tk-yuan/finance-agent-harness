"""权限边界与审计（对应教程 s03）。

三件事：

1. **工作区免问，区外要人点头**。
   `workspace/`、`data/` 下随便读写（这是本项目自己的地盘）；
   想碰区外的文件，不在循环里硬拒，而是**推一条审批请求问用户**——
   允许就放行（本次进程内记住），拒绝才拦。
   这跟 Claude Code 的模型是一回事：常用的别烦人，越界的必须问。

2. **工具分级**。标了 `read_only=True` 的工具可以随意调用；
   写操作要过额外检查。

3. **审计留痕**。每一次拒绝、每一次"越界获批"都写进 `.cache/audit.jsonl`。
   对应 JD 里的「操作拦截和审计上报」——这里落到本地文件，
   真实企业环境里是上送到链路追踪平台。

**为什么改掉了原来的"越界一律拒"**：那条规则安全，但也让 Agent 残废——
用户问"看看我桌面那个 Excel"，它只会回"路径越界"，一点忙帮不上。
真正可用的边界不是"绝对不许"，而是**"每次都要人明确同意"**。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from ..config import AUDIT_LOG, FS_ALLOWED_ROOTS

# 审计日志会被并发写入（一轮内多个工具调用并行执行），加锁避免行交错
_AUDIT_LOCK = threading.Lock()


class PermissionDenied(Exception):
    """越权操作。被主循环捕获后作为工具错误返回给模型。"""


def audit(event: str, **fields) -> None:
    """追加一条审计记录。并发安全。"""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT_LOCK, AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(line)


class PermissionPolicy:
    def __init__(self, allowed_roots=None, allow_write: bool = True, gate=None) -> None:
        self.allowed_roots = tuple(Path(p).resolve() for p in (allowed_roots or FS_ALLOWED_ROOTS))
        self.allow_write = allow_write
        # 传了 gate 就开启"越界问人"。CLI / 单测不传 → 维持"越界即拒"的老行为。
        self.gate = gate
        self._granted: set[Path] = set()      # 本次进程内已被用户点过"允许"的路径
        self._lock = threading.Lock()

    # —— 路径 ——
    def _inside(self, p: Path) -> bool:
        return any(p == r or r in p.parents for r in self.allowed_roots)

    def check_path(self, raw: str, write: bool = False, tool: str = "") -> Path:
        """解析路径并校验。区外路径会**问用户**（有 gate 时），而不是直接拒。"""
        p = Path(raw)
        if not p.is_absolute():
            p = self.allowed_roots[0] / p
        p = p.resolve()

        if not self._inside(p) and not self._already_granted(p):
            if self.gate is None or not self._ask(p, write, tool):
                audit("path_denied", path=str(p), write=write)
                raise PermissionDenied(
                    f"用户没有允许访问 {p}。工作区内的文件（{'、'.join(str(r) for r in self.allowed_roots)}）"
                    f"可以直接读写；区外需要用户逐次授权。"
                )

        if write and not self.allow_write:
            audit("write_denied", path=str(p))
            raise PermissionDenied("当前会话为只读模式，禁止写入。")
        return p

    def _already_granted(self, p: Path) -> bool:
        with self._lock:
            return p in self._granted

    def _ask(self, p: Path, write: bool, tool: str) -> bool:
        """推一条审批请求，阻塞等用户点"允许/拒绝"。"""
        what = "写入" if write else "读取"
        req = self.gate.request(
            tool_name=tool or ("write_file" if write else "read_file"),
            args={"path": str(p)},
            reason=f"这个路径在工作区之外，需要你确认是否允许 Agent {what}",
            risk="high" if write else "medium",
        )
        ok = req.status == "approved"
        audit("path_approval", path=str(p), write=write,
              status=req.status, decider=req.decider)
        if ok:
            with self._lock:
                self._granted.add(p)
        return ok

    def check_tool(self, tool, args: dict) -> None:
        """工具级检查：只读工具直接放行，写操作需通过路径校验。"""
        if getattr(tool, "read_only", True):
            return
        target = args.get("path") or args.get("file") or ""
        if target:
            self.check_path(str(target), write=True, tool=tool.name)
        audit("tool_allowed", tool=tool.name, write=True, args=target)


def default_policy() -> PermissionPolicy:
    return PermissionPolicy()
