"""装配：把所有零件拼成一个可运行的主 Agent。

这是唯一一处"知道全局"的代码。其余模块彼此不直接耦合，
只通过 ToolRegistry / HookManager / RunState 三个接口协作。
换掉任何一个零件（比如把自建检索换成向量库），只改这里。
"""

from __future__ import annotations

from .config import DATA_DIR, MODEL, SKILLS_DIR, VIRTUAL_FS, WORKSPACE_DIR
from .context.memory import LayeredMemory
from .llm import LLMClient
from .observability.trace import Trace
from .plugins.builtin_workflows import register_builtin_workflows
from .plugins.goal import GoalChecker
from .plugins.mcp import build_demo_servers
from .plugins.workflow import WorkflowRuntime
from .runtime.hooks_impl import build_hooks
from .runtime.loop import Agent, RunState
from .runtime.middleware import LatencyTracker, ToolCache
from .runtime.permissions import PermissionPolicy
from .runtime.tools import Tool, ToolRegistry
from .skills.loader import SkillLibrary, register_skill_tools
from .tasks.background import shared_manager
from .tasks.scheduler import shared_scheduler
from .tasks.subagent import SubagentRunner, default_specs, register_task_tool
from .tasks.team import Team
from .tasks.todo import TaskBoard
from .tools.builtin import register_fs_tools
from .tools.finance import register_finance_tools
from .tools.kb import register_kb_tools
from .tools.async_tasks import register_async_tools, register_schedules
from .tools.ledger import register_ledger_tools
from .tools.human import register_human_tools
from .tools.team_tools import register_team_tools

MAIN_SYSTEM = """你是一家小微企业的财务助手。你的职责是帮出纳和会计把账记准、查清、核好。

**最重要的一条：你没有人事权和签字权。**

整个财务系统的内控基础是「出纳登记 → 会计审核」两道工序——
一个人既管钱又管账，挪用了公款自己改账就没人发现。
AI 介入后这个校验点更缺，所以：

- 你能**登记**单据（`submit_voucher`），登记后是「待审核」状态，不计入报表；
- 你能**建议**科目，但**入账（`post_voucher`）会触发人工审批**——会计签字才算数；
- 你能**稽核**账目，但稽核不是修改，发现问题要说出来而不是自己改掉。

工作原则：

1. **数字必须来自工具**。任何金额、余额、合计都要调工具算，不许估算或心算。

2. **科目判断要有依据，判断不了就问。** 不确定一笔支出该记什么科目时，
   先用 `search_accounting_rules` 查准则，或委派 `rule_researcher`。
   但**准则也定不了、属于业务性质的问题**——这笔采购到底是耗材还是成本、
   这张发票是不是重复报销、这笔款对应哪个合同——用 `ask_user` 直接问用户。
   **宁可问一句，不要凭"感觉应该算办公费"下结论。**
   注意分寸：能从账套和准则查到的一律自己查，`ask_user` 不是偷懒的出口。

3. **记账必须有凭证**。没有发票/回单的支出不允许登记——白条入账税务不认。
   工具会拦你，但你不该等到被拦才想起来。

4. **区分余额和流水**。`query_entries` 回答"这段时间花了多少"（流量），
   `get_account_balance` 回答"账上还剩多少"（存量）。问的是哪个就答哪个。

5. **大产出落盘**。报表、分析报告用 write_file 写进工作区对应分区，
   对话里只留摘要和路径。

   工作区布局（请把文件放到对应分区）：

{fs_layout}

6. **该委派就委派**。查会计处理依据、独立稽核、多维度财务分析，
   用 task 工具交给子 Agent，只收回结论。

7. **收工前自查**。任务清单没做完不许收工；账目有问题要如实说，不要报喜不报忧。
"""

_FS_LAYOUT = "\n".join(f"     workspace/{k}/   —— {v}" for k, v in VIRTUAL_FS.items())
MAIN_SYSTEM = MAIN_SYSTEM.replace("{fs_layout}", _FS_LAYOUT)


# ═══════════════ 任务清单工具 ═══════════════

def _norm_id(raw) -> str:
    """把任务编号归一化。

    模型经常写 "1" 而不是 "T1"（清单里显示的是 T1，它记住了序号）。
    不归一化的话，`blocked_by=["1"]` 永远匹配不上，任务被永久阻塞。
    """
    s = str(raw or "").strip().upper()
    return f"T{s}" if s and s[0].isdigit() else s


def _board_action(board: TaskBoard, action: str, task_id: str, subject: str,
                  blocked_by, result: str) -> dict:
    """任务清单操作。每次都把整张清单连结果一起返回，让模型始终看到真实状态。"""
    task_id = _norm_id(task_id)
    deps = [_norm_id(b) for b in (blocked_by or [])]

    if action == "add":
        if not subject:
            return {"error": "add 需要 subject", "board": board.render()}
        unknown = [d for d in deps if board._find(d) is None]
        if unknown:
            return {"error": f"blocked_by 引用了不存在的任务：{unknown}",
                    "board": board.render()}
        t = board.add(subject, deps or None)
        return {"ok": True, "task_id": t.id, "board": board.render(),
                "progress": board.progress()}

    if action == "view":
        return {"board": board.render(), "tasks": board.to_dict(),
                "progress": board.progress()}

    if not task_id:
        return {"error": f"{action} 需要 task_id（形如 T1）", "board": board.render()}

    if action == "start":
        out = board.start(task_id)
    elif action == "complete":
        out = board.complete(task_id, result)
    elif action == "fail":
        out = board.fail(task_id, result)
    else:
        return {"error": f"未知 action：{action}", "board": board.render()}

    out["board"] = board.render()
    out["progress"] = board.progress()
    return out


def register_board_tool(registry: ToolRegistry, board: TaskBoard) -> None:
    registry.register(Tool(
        name="task_board",
        description="任务清单操作。action: add / start / complete / fail / view。"
                    "新建时 blocked_by 传前置任务 id 列表；前置未完成的任务不能 start。",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "start", "complete", "fail", "view"]},
            "task_id": {"type": "string"}, "subject": {"type": "string"},
            "blocked_by": {"type": "array", "items": {"type": "string"}},
            "result": {"type": "string"},
        }, "required": ["action"]},
        handler=lambda action, task_id="", subject="", blocked_by=None, result="":
            _board_action(board, action, task_id, subject, blocked_by, result),
        read_only=True, category="task",
    ))


# ═══════════════ 装配 ═══════════════

def build_harness(goal: str = "", trace: Trace | None = None,
                  goal_condition: str = "", approval_gate=None) -> dict:
    """组装一个完整的主 Agent。

    approval_gate 传入后启用「人工审批门」：`post_voucher`（入账）会阻塞等人批复。
    CLI 模式传一个控制台提示的 gate，服务模式传一个经 SSE 推给前端的 gate。
    """
    registry = ToolRegistry()
    policy = PermissionPolicy()
    board = TaskBoard()
    memory = LayeredMemory()
    skills = SkillLibrary(SKILLS_DIR)
    llm = LLMClient()

    # —— 领域工具 ——
    register_ledger_tools(registry)      # 出纳 + 会计（含内控约束）
    register_finance_tools(registry)     # 余额、往来、对账、结账、报表
    register_kb_tools(registry)          # 财会准则检索
    register_fs_tools(registry, policy)  # 虚拟文件系统
    register_human_tools(registry, approval_gate)   # 向人提问（审批门的"问答"形态）
    register_skill_tools(registry, skills)
    register_board_tool(registry, board)

    agent = Agent(registry, policy=policy, llm=llm, system_prompt=MAIN_SYSTEM,
                  trace=trace, state=RunState(), approval_gate=approval_gate)

    # —— 子 Agent 委派（一次性）——
    runner = SubagentRunner(agent, default_specs())
    register_task_tool(registry, runner)

    # —— Agent Teams（持久队友）——
    team = Team(agent)
    register_team_tools(registry, team)

    # —— s11 后台任务 + s12 定时调度 ——
    bg = shared_manager()
    sched = shared_scheduler()
    register_schedules(sched, bg)
    register_async_tools(registry, bg, sched)

    # —— 插件：MCP 外部工具 ——
    mcp_clients = build_demo_servers(DATA_DIR)
    mcp_added: list[str] = []
    for client in mcp_clients:
        mcp_added += client.connect(registry)

    # —— 插件：Workflow 确定性编排 ——
    workflows = WorkflowRuntime(runner)
    register_builtin_workflows(workflows)
    registry.register(Tool(
        name="run_workflow",
        description="运行一个预定义的固定流程（宿主注册的可信脚本，模型只能选名字和参数）。"
                    "适用于已知步骤顺序的任务，比逐轮自行决策更快更稳，且中断可续跑。",
        parameters={"type": "object", "properties": {
            "name": {"type": "string"}, "args": {"type": "object"},
            "run_id": {"type": "string"}},
            "required": ["name"]},
        handler=lambda name, args=None, run_id=None: workflows.run(name, args, run_id),
        read_only=False, category="workflow",
    ))

    # —— 插件：目标闭环判定器 ——
    goal_checker = GoalChecker(llm, condition=goal_condition)
    wf_hint = ("【可用 workflow】\n"
               + "\n".join(f"- {w['name']}：{w['description']}" for w in workflows.catalog())
               + "\n（步骤顺序固定的任务优先用 run_workflow）")

    tool_cache = ToolCache()
    latency = LatencyTracker()

    hooks = build_hooks(
        base_prompt=MAIN_SYSTEM, memory=memory, board=board,
        team=team, background=bg,
        skill_catalog=skills.catalog(),
        subagent_catalog=runner.catalog() + "\n\n" + wf_hint,
        llm=llm, trace=trace, goal_checker=goal_checker,
        tool_cache=tool_cache, latency=latency,
    )
    agent.hooks = hooks

    return {
        "agent": agent, "registry": registry, "policy": policy, "board": board,
        "memory": memory, "skills": skills, "runner": runner, "llm": llm,
        "hooks": hooks, "workflows": workflows, "goal_checker": goal_checker,
        "approval_gate": approval_gate, "cache": tool_cache, "latency": latency,
        "mcp_tools": mcp_added, "workspace": WORKSPACE_DIR, "model": MODEL,
        "team": team, "background": bg, "scheduler": sched,
    }
