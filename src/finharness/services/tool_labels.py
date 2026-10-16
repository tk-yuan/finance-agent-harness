"""工具名 → 中文短名。

界面要写人话：「月度对账」比 `reconcile_month` 好读。业务界面的动作标签
和 /tools 接口共用这一份，免得两边各维护一套、改了不同步。

放在 services/ 而不是某个界面文件里，是因为它有两个消费方：
业务界面的前端（经 /tools 拿）和运行时的工具池展示。
"""

from __future__ import annotations

TOOL_LABELS: dict[str, tuple[str, str]] = {
    "submit_voucher": ("登记单据", "record"),
    "withdraw_voucher": ("撤回单据", "delete"),
    "post_voucher": ("审核入账", "approve"),
    "reject_voucher": ("驳回单据", "approve"),
    "recategorize": ("改科目", "approve"),
    "query_entries": ("查账", "query"),
    "get_account_balance": ("查余额", "query"),
    "query_counterparty": ("查往来", "query"),
    "reconcile_month": ("对账", "reconcile"),
    "close_period": ("月末结账", "reconcile"),
    "generate_report": ("出报表", "query"),
    "search_accounting_rules": ("查准则", "query"),
    "get_current_time": ("查日期", "query"),
    "days_between": ("算天数", "query"),
    "task": ("委派子任务", "delegate"),
    "spawn_teammate": ("招募队友", "team"),
    "assign_task": ("派活", "team"),
    "team_board": ("看团队", "team"),
    "background_run": ("提交后台任务", "background"),
    "background_status": ("查后台", "background"),
    "run_workflow": ("跑固定流程", "workflow"),
    "write_file": ("写文件", "fs"),
    "read_file": ("读文件", "fs"),
    "ask_user": ("向你提问", "ask"),
    "read_image": ("看图片", "query"),
    "remember": ("记经验", "memory"),
    "run_command": ("执行命令", "shell"),
    "task_board": ("看任务板", "delegate"),
    "list_pending": ("查待审核", "query"),
    "list_closed_periods": ("查已结账期间", "query"),
    "list_dir": ("列目录", "fs"),
    "search_files": ("搜文件", "fs"),
    "load_skill": ("加载技能", "chat"),
    "list_schedules": ("查定时任务", "background"),
    "run_schedules_now": ("立即跑定时任务", "background"),
    "send_message": ("给队友发消息", "team"),
    "shutdown_teammate": ("解散队友", "team"),
    "mcp_rule_detail": ("查规则详情", "query"),
    "mcp_rule_stats": ("查规则统计", "query"),
    "mcp_to_capital": ("金额转大写", "query"),
    "mcp_yuan_to_wan": ("元转万元", "query"),
}
