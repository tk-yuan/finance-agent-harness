"""MCP 工具接入（对应教程 s14）：动态工具池。

MCP 把两件事解耦了：
    「工具有哪些」  —— 由 server 提供
    「工具怎么被用」—— 由 Harness 负责连接、命名、权限检查

于是新增一个数据源不必改 Harness 代码：连上 server，把它的工具并进工具池即可。

**本实现是进程内模拟 server**（与教程一致），演示 `tools/list` 与 `tools/call`
两个核心动作，以及工具池的动态组装。真实的 stdio / HTTP transport
不在本模块范围内——那是协议实现，不是 Agent 能力。

安全要点：**外部来源的工具默认按非只读处理**。
来自外部的工具不能默认可信——这是 MCP 部署里最容易出事的地方。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable

from ..runtime.tools import Tool, ToolRegistry


@dataclass
class MCPServerInfo:
    name: str
    version: str = "1.0"
    description: str = ""


class InProcessMCPServer:
    """一个进程内的模拟 MCP server。"""

    def __init__(self, info: MCPServerInfo) -> None:
        self.info = info
        self._tools: dict[str, dict] = {}

    def add_tool(self, name: str, description: str, parameters: dict, fn: Callable) -> None:
        self._tools[name] = {"spec": {"name": name, "description": description,
                                      "inputSchema": parameters}, "fn": fn}

    # —— MCP 协议的两个核心动作 ——
    def list_tools(self) -> list[dict]:
        return [t["spec"] for t in self._tools.values()]

    def call_tool(self, name: str, arguments: dict) -> dict:
        if name not in self._tools:
            return {"isError": True, "content": f"server 上没有工具 {name}"}
        try:
            out = self._tools[name]["fn"](**arguments)
            return {"isError": False,
                    "content": out if isinstance(out, str)
                    else json.dumps(out, ensure_ascii=False, default=str)}
        except Exception as e:  # noqa: BLE001
            return {"isError": True, "content": f"{type(e).__name__}: {e}"}


@dataclass
class MCPClient:
    """连接一个 server，把它的工具转成 Harness 能用的 Tool。"""

    server: InProcessMCPServer
    namespace: str = ""

    def discover(self) -> list[Tool]:
        tools = []
        for spec in self.server.list_tools():
            name = f"{self.namespace}{spec['name']}" if self.namespace else spec["name"]
            tools.append(Tool(
                name=name,
                description=f"[来自 {self.server.info.name}] {spec['description']}",
                parameters=spec.get("inputSchema", {"type": "object", "properties": {}}),
                handler=self._wrap(spec["name"]),
                # 外部工具默认不可信：按非只读处理，走权限检查
                read_only=False, category="mcp",
            ))
        return tools

    def _wrap(self, remote_name: str) -> Callable:
        def _call(**kwargs):
            result = self.server.call_tool(remote_name, kwargs)
            return {"error": result["content"]} if result.get("isError") else result["content"]

        _call.__name__ = f"mcp_{remote_name}"
        return _call

    def connect(self, registry: ToolRegistry) -> list[str]:
        """连上并把工具并进工具池，返回新加入的工具名。"""
        added = []
        for tool in self.discover():
            if registry.get(tool.name) is not None:
                continue
            registry.register(tool)
            added.append(tool.name)
        return added


# ═══════════════ 演示用的两个 server ═══════════════

DIGITS = "零壹贰叁肆伍陆柒捌玖"
_UNITS = ["", "拾", "佰", "仟"]
_BIG = ["", "万", "亿", "万亿"]


def _int_to_capital(n: int) -> str:
    """整数部分转大写。

    按「四位一节」处理，两个细节决定对不对：

      ① **整节为零时不写节权**。1 亿的万节、个节全为零，
         不能输出「壹亿万元」——万节为零就不该出现「万」字。
      ② **节内有前导零要补一个「零」**。10 万零 800 中，
         800 不足四位，前面必须补「零」，否则读成「壹拾万捌佰」，
         金额上差之毫厘谬以千里。
    """
    if n == 0:
        return "零"

    sections: list[int] = []
    while n > 0:
        sections.append(n % 10000)
        n //= 10000

    parts: list[str] = []
    for i in range(len(sections) - 1, -1, -1):
        g = sections[i]
        if g == 0:
            # 该节全零：只有当它上面还有非零节时才补一个「零」占位
            if parts and any(sections[:i]):
                parts.append("零")
            continue

        seg, pending_zero = "", False
        for j in range(3, -1, -1):
            d = (g // (10 ** j)) % 10
            if d == 0:
                pending_zero = True
            else:
                if pending_zero and seg:
                    seg += "零"
                seg += DIGITS[d] + _UNITS[j]
                pending_zero = False

        if i < len(sections) - 1 and g < 1000:
            seg = "零" + seg          # ② 节内前导零
        parts.append(seg + _BIG[i])

    t = "".join(parts)
    t = re.sub(r"零+", "零", t)
    return t.rstrip("零") or "零"


def _to_capital(amount: float) -> str:
    """金额转人民币大写（报销单、发票、支票都要写大写）。

    这是财务域里**确定性算法**的典型代表：规则完全确定，但手写极易出错——
    模型经常把「壹拾」写成「拾」，或者漏掉「壹拾万**零**捌佰」中间那个零。
    所以它必须由代码实现，而且要有测试锁住。
    """
    if amount < 0:
        return "负" + _to_capital(-amount)

    # 先四舍五入到分再取整，避免浮点误差把 0.56 变成 55.999
    cents = int(round(amount * 100))
    integer, decimal = divmod(cents, 100)

    head = "人民币" + _int_to_capital(integer) + "元"
    if decimal == 0:
        return head + "整"

    jiao, fen = divmod(decimal, 10)
    if jiao == 0:
        return f"{head}零{DIGITS[fen]}分"      # 角为 0、分不为 0
    if fen == 0:
        return f"{head}{DIGITS[jiao]}角整"      # 分为 0
    return f"{head}{DIGITS[jiao]}角{DIGITS[fen]}分"


def build_demo_servers(data_dir) -> list[MCPClient]:
    """构造两个演示 server：准则库查询、金额处理。

    真实场景里这两个 server 会由别的团队维护、跑在别的进程；
    这里进程内模拟，只为演示「工具池可动态扩展」这件事本身。
    """
    from pathlib import Path

    data_dir = Path(data_dir)

    def _corpus() -> list[dict]:
        p = data_dir / "corpus.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    rule_server = InProcessMCPServer(
        MCPServerInfo(name="rule-registry", description="财会准则库只读查询服务")
    )

    def rule_stats() -> dict:
        rows = _corpus()
        by_year: dict[str, int] = {}
        for r in rows:
            y = (r.get("issued") or "????")[:4]
            by_year[y] = by_year.get(y, 0) + 1
        return {"total_documents": len(rows),
                "by_year": dict(sorted(by_year.items(), key=lambda kv: -kv[1])[:10]),
                "with_doc_no": sum(1 for r in rows if r.get("doc_no")),
                "with_effective_date": sum(1 for r in rows if r.get("effective_from"))}

    def rule_detail(doc_no: str) -> dict:
        for r in _corpus():
            if doc_no and doc_no in (r.get("doc_no") or ""):
                return {"title": r.get("title"), "doc_no": r.get("doc_no"),
                        "issued": r.get("issued"),
                        "effective_from": r.get("effective_from") or "未标注",
                        "chars": r.get("chars")}
        return {"error": f"未找到文号含「{doc_no}」的准则"}

    rule_server.add_tool("rule_stats", "统计准则库的规模与年份分布",
                         {"type": "object", "properties": {}}, rule_stats)
    rule_server.add_tool("rule_detail", "按文号查一条准则的元数据",
                         {"type": "object", "properties": {
                             "doc_no": {"type": "string"}}, "required": ["doc_no"]},
                         rule_detail)

    amount_server = InProcessMCPServer(
        MCPServerInfo(name="amount-tools", description="财务金额处理服务")
    )
    amount_server.add_tool("to_capital", "金额转人民币大写（报销单/发票用）",
                           {"type": "object", "properties": {
                               "amount": {"type": "number"}}, "required": ["amount"]},
                           lambda amount: _to_capital(float(amount)))
    amount_server.add_tool("yuan_to_wan", "元转万元",
                           {"type": "object", "properties": {
                               "yuan": {"type": "number"}}, "required": ["yuan"]},
                           lambda yuan: {"wan": round(float(yuan) / 10000, 2)})

    return [
        MCPClient(rule_server, namespace="mcp_"),
        MCPClient(amount_server, namespace="mcp_"),
    ]
