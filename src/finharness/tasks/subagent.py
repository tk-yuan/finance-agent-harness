"""财务子 Agent：按职责分工，各自独立上下文。

## 为什么财务域特别适合子 Agent

财务工作的本质就是**分工**——出纳管钱、会计管账、独立第三方审计。
如果把所有工具都给一个 Agent，它既能录单又能审核，那内控就白设了。

所以这里的角色划分**不是技术炫技，是照着真实岗位分的**：

| 角色 | 对应岗位 | 工具范围 | 关键约束 |
|---|---|---|---|
| `rule_researcher` | 制度查询 | 只读 + 准则检索 | 只给依据，不给结论 |
| `ledger_auditor` | 内部稽核 | **只读** | **刻意不给写权限**——能改账的稽核没有意义 |
| `finance_analyst` | 财务分析 | 只读 + 写报告 | 只做分析，不做账务处理 |

**核心价值是上下文隔离**：稽核员可能翻几十条明细、查十几份准则，
这些原始材料一行都不进主 Agent 的上下文，只收到一段结论。

## 和 `finance-agent` 原版的区别

原版用 LangGraph 的 StateGraph 编排四个分析角色。
这里换成运行时的子 Agent 机制——差别在于：
**子 Agent 的工具权限是运行时收窄的**（`allow` 集合），
而不是靠"记得别调那个工具"。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import MAX_SUBAGENT_TURNS
from ..domain.roles import EPHEMERAL, ROLES
from ..runtime.loop import Agent, RunState
from ..runtime.tools import Tool, ToolRegistry

# —— 工具分组：按"能造成什么后果"分，不按功能分 ——
READ_ONLY = {
    "query_entries", "get_account_balance", "query_counterparty",
    "list_pending", "list_closed_periods", "get_current_time", "days_between",
    "read_file", "list_dir", "search_files", "load_skill", "search_accounting_rules",
}
WRITE_DRAFT = {"submit_voucher", "withdraw_voucher", "write_file"}


@dataclass
class SubagentSpec:
    name: str
    description: str
    system_prompt: str
    allow: set[str] | None = None


def default_specs() -> dict[str, SubagentSpec]:
    return {
        "rule_researcher": SubagentSpec(
            name="rule_researcher",
            description="制度研究员：检索会计准则与会计制度，给出某项业务的会计处理依据",
            system_prompt=ROLES["researcher"].duties + EPHEMERAL,
            allow=READ_ONLY | WRITE_DRAFT,
        ),
        "ledger_auditor": SubagentSpec(
            name="ledger_auditor",
            description="账务稽核员（一次性委派）：独立复核指定的这一批账目（只读，不给写权限）",
            system_prompt=ROLES["auditor"].duties + EPHEMERAL,
            # ★ 稽核员刻意不给任何写权限——能改账的稽核等于没有稽核
            allow=READ_ONLY,
        ),
        "finance_analyst": SubagentSpec(
            name="finance_analyst",
            description="财务分析师：从盈利、偿债、营运、风险四个维度分析经营状况",
            system_prompt=ROLES["analyst"].duties + EPHEMERAL,
            allow=READ_ONLY | {"write_file"},
        ),
    }


class SubagentRunner:
    """用主 Agent 的依赖克隆出子 Agent，跑完只返回结论。"""

    def __init__(self, base: Agent, specs: dict[str, SubagentSpec] | None = None,
                 max_turns: int = MAX_SUBAGENT_TURNS) -> None:
        self.base = base
        self.specs = specs or default_specs()
        self.max_turns = max_turns
        self.records: list[dict] = []

    def catalog(self) -> str:
        lines = ["【可委派的子 Agent】（用 task 工具委派，只收回结论，不污染主上下文）"]
        lines += [f"- {s.name}：{s.description}" for s in self.specs.values()]
        return "\n".join(lines)

    def run(self, agent: str, goal: str) -> dict:
        """参数名必须与工具 schema 里的 `agent` / `goal` 一致。

        踩过的坑：方法签名原本是 `run(name, goal)`，而工具 schema 声明的是
        `agent`，模型按 schema 传 `agent=...` 就报 unexpected keyword argument。
        工具名对得上、参数名对不上，是最隐蔽的一类接线错误。
        """
        spec = self.specs.get(agent)
        if spec is None:
            return {"error": f"未知子 Agent：{agent}", "available": list(self.specs)}

        child = RunState(goal=goal, depth=self.base.state.depth + 1,
                         parent_goal=self.base.state.goal)
        sub = self.base.clone(
            system_prompt=spec.system_prompt, allow=spec.allow,
            max_turns=self.max_turns, state=child, name=spec.name,
        )

        if self.base.trace is not None:
            with self.base.trace.span("subagent", spec.name, goal=goal[:120]) as span:
                result = sub.run(goal)
                span["fields"]["turns"] = result.turns
        else:
            result = sub.run(goal)

        rec = {"agent": spec.name, "goal": goal, "conclusion": result.answer,
               "turns": result.turns, "stop_reason": result.stop_reason}
        self.records.append(rec)
        return rec


def register_task_tool(registry: ToolRegistry, runner: SubagentRunner) -> None:
    registry.register(Tool(
        name="task",
        description=(
            "把一个可独立完成的子任务委派给子 Agent，只收回结论（不返回中间过程）。"
            "适用于：查某项业务的会计处理依据、独立稽核一批账目、做多维度财务分析。"
            "委派时必须把背景写进 goal——子 Agent 看不到当前对话历史。"
        ),
        parameters={"type": "object", "properties": {
            "agent": {"type": "string", "description": "子 Agent 名称，见系统提示里的目录"},
            "goal": {"type": "string", "description": "子任务的完整描述，含必要背景与期望输出"},
        }, "required": ["agent", "goal"]},
        handler=lambda agent, goal: runner.run(agent, goal),
        read_only=True, category="delegate",
    ))
