"""Agent Teams 的工具接口与财务角色定义。

## 计划闸门怎么实现的

s13 要求：Lead 必须先提出分工方案、**等用户确认**才能启动队友
（因为启动队友会改变成本、并发度和能改工作区的角色集合）。

本项目用一个现成的机制实现它：**`spawn_teammate` 是高风险工具，走人工审批门**。
于是"用户确认"不再是提示词里的一句请求，而是运行时的硬门槛——
不批准就启不了队友。这和 `post_voucher`（入账要会计签字）用的是同一套机制。

## 财务域的四个角色

| 队友 | 岗位 | 能干什么 | 关键约束 |
|---|---|---|---|
| 出纳 | 登记收付款 | 提交/撤回待审核单据 | **不能入账** |
| 会计 | 审核入账 | 核科目、审核、驳回 | **入账要走人工审批** |
| 稽核 | 独立复核 | 只读查账、找异常 | **无任何写权限** |
| 分析师 | 经营分析 | 算指标、出报告 | 不做账务处理 |
"""

from __future__ import annotations

from ..domain.roles import PERSISTENT, ROLES
from ..runtime.tools import Tool, ToolRegistry
from ..tasks.team import Team, TeammateSpec

# —— 工具分组 ——
READ = {
    "query_entries", "get_account_balance", "query_counterparty", "list_pending",
    "list_closed_periods", "get_current_time", "days_between", "read_file",
    "list_dir", "search_files", "load_skill", "search_accounting_rules",
    "team_board", "send_message",
}
DRAFT = {"submit_voucher", "withdraw_voucher", "write_file"}
REVIEW = {"post_voucher", "reject_voucher", "recategorize"}


def default_teammate_specs() -> dict[str, TeammateSpec]:
    return {
        "cashier_zhang": TeammateSpec(
            name="cashier_zhang", role="出纳",
            system_prompt=ROLES["cashier"].duties + PERSISTENT,
            allow=READ | DRAFT,
        ),
        "accountant_li": TeammateSpec(
            name="accountant_li", role="会计",
            system_prompt=ROLES["accountant"].duties + PERSISTENT,
            allow=READ | REVIEW,
        ),
        "auditor_wang": TeammateSpec(
            name="auditor_wang", role="稽核员",
            system_prompt=ROLES["auditor"].duties + PERSISTENT,
            allow=READ,
        ),
        "analyst_zhao": TeammateSpec(
            name="analyst_zhao", role="财务分析师",
            system_prompt=ROLES["analyst"].duties + PERSISTENT,
            allow=READ | {"write_file"},
        ),
    }


def register_team_tools(registry: ToolRegistry, team: Team) -> None:
    def spawn_teammate(name: str, role: str = "") -> dict:
        """招募队友。**这是高风险工具，会触发人工审批。**

        s13 要求 Lead 先提方案、等用户确认才能启动队友。
        本项目把"用户确认"做成了审批门——不批就启不了。
        """
        specs = default_teammate_specs()
        spec = specs.get(name)
        if spec is None:
            if not role:
                return {"error": f"未知队友模板：{name}", "available": list(specs)}
            spec = TeammateSpec(name=name, role=role,
                                system_prompt=f"你是{role}。完成分配给你的任务，"
                                              f"结果用 send_message 回报 lead。",
                                allow=READ)
        return team.spawn(spec)

    def assign_task(teammate: str, subject: str, prompt: str = "") -> dict:
        return team.assign(teammate, subject, prompt)

    def send_message(to: str, content: str) -> dict:
        team.bus.send("lead", to, content)
        return {"ok": True, "to": to}

    registry.register(Tool(
        name="spawn_teammate",
        description="招募一个**持久队友**。它有自己的上下文，会在空闲时自己认领任务，"
                    "跨任务保留记忆。启动队友会改变成本与并发度，"
                    "所以本工具**会触发人工审批**——必须先获得确认。"
                    "可用模板：" + "、".join(
                        f"{k}（{v.role}）" for k, v in default_teammate_specs().items()),
        parameters={"type": "object", "properties": {
            "name": {"type": "string", "description": "队友模板名，见工具描述"},
            "role": {"type": "string", "description": "自定义角色（用非模板名时）"},
        }, "required": ["name"]},
        handler=spawn_teammate, read_only=False, requires_approval=True,
        category="team",
    ))
    registry.register(Tool(
        name="assign_task",
        description="给指定队友派一个任务。任务会同时写进共享任务板。",
        parameters={"type": "object", "properties": {
            "teammate": {"type": "string"}, "subject": {"type": "string"},
            "prompt": {"type": "string", "description": "详细说明（含必要背景）"},
        }, "required": ["teammate", "subject"]},
        handler=assign_task, read_only=False, category="team",
    ))
    registry.register(Tool(
        name="send_message",
        description="给某个队友发消息（队友也在各自的收件箱里收）。",
        parameters={"type": "object", "properties": {
            "to": {"type": "string"}, "content": {"type": "string"},
        }, "required": ["to", "content"]},
        handler=send_message, read_only=False, category="team",
    ))
    registry.register(Tool(
        name="team_board",
        description="查看团队状态：每个队友在忙什么、共享任务板上还有哪些活、"
                    "哪些已被认领。用于掌握团队全局，避免重复派活。",
        parameters={"type": "object", "properties": {}},
        handler=team.list_members, read_only=True, category="team",
    ))
    registry.register(Tool(
        name="shutdown_teammate",
        description="让某个队友关机（团队工作结束时调用，避免线程常驻）。",
        parameters={"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]},
        handler=team.shutdown, read_only=False, category="team",
    ))
