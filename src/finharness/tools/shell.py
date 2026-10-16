"""命令行工具：让 Agent 能在本机执行命令。

**每一次调用都必须人工批准**（工具标了 `requires_approval`），
审批卡片上会**原样显示要执行的命令**，人看清楚再点允许——
这就是 Claude Code 的那套模型：不是不许，是每次都要你点头。

## 为什么财务 Agent 需要它

账套之外的事比账套里多：核对一个导出的对账单、看看客户发来的发票扫描件在不在、
把一堆 Excel 转成 CSV。没有这个工具，Agent 遇到这类需求只能说"你自己去弄"。

## 安全边界在哪

**完全落在"人有没有点那个批准按钮"上**，所以有三条硬约束：

1. 命令原样展示，不截断、不加省略号——看不懂的命令没人敢批，也就等于没拦
2. 超时强制结束，不留挂死的进程
3. 执行前后都写审计日志（`permissions.audit`）

默认开启（因为是逐次审批的），要关掉就把 `.env` 里的 `HARNESS_ALLOW_SHELL` 设成 0。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..config import ALLOW_SHELL, WORKSPACE_DIR
from ..runtime.permissions import audit
from ..runtime.tools import Tool, ToolRegistry

MAX_OUTPUT = 4000
DEFAULT_TIMEOUT = 60


def register_shell_tools(registry: ToolRegistry) -> None:
    if not ALLOW_SHELL:
        return

    def run_command(command: str, cwd: str = "", timeout: int = DEFAULT_TIMEOUT) -> dict:
        workdir = Path(cwd).expanduser() if cwd else WORKSPACE_DIR
        if not workdir.is_dir():
            return {"error": f"工作目录不存在：{workdir}"}
        timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), 300))

        audit("command_start", command=command, cwd=str(workdir))
        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(workdir), timeout=timeout,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            audit("command_timeout", command=command, timeout=timeout)
            return {"error": f"命令超过 {timeout} 秒未结束，已强制终止。"}
        except Exception as e:  # noqa: BLE001 —— 执行失败也是业务结果，不该炸掉整轮
            audit("command_error", command=command, error=type(e).__name__)
            return {"error": f"命令执行失败：{type(e).__name__}: {e}"}

        audit("command_done", command=command, returncode=proc.returncode)
        out = (proc.stdout or "")[-MAX_OUTPUT:]
        err = (proc.stderr or "")[-1500:]
        return {
            "command": command,
            "cwd": str(workdir),
            "returncode": proc.returncode,
            "stdout": out or "（无输出）",
            "stderr": err,
        }

    registry.register(Tool(
        name="run_command",
        description=(
            "在本机执行一条 shell 命令并拿到输出。\n"
            "**每次调用都会弹给人批准**，所以：命令要写清楚完整的一条，别人一看就知道它干什么。\n"
            "适用：查看文件内容/目录、统计与格式转换（wc/grep/sort/python 脚本）、"
            "处理账套之外的文件。\n"
            "不适用：账套里查得到的一律用查账工具，不要绕道命令行；"
            "不要用 rm / 覆盖类命令——需要删改文件先说清楚为什么。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "完整的命令行，会原样展示给人确认"},
                "cwd": {"type": "string", "description": "工作目录，默认 workspace/"},
                "timeout": {"type": "integer", "description": "超时秒数，默认 60，上限 300"},
            },
            "required": ["command"],
        },
        handler=run_command,
        read_only=False,
        category="shell",
        # ★ 高风险：每一次都要人批准
        requires_approval=True,
    ))
