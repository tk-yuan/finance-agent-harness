"""岗位只定义一次：子 Agent 和队友是同一个角色的两种部署方式。"""

from finharness.domain.roles import EPHEMERAL, PERSISTENT, ROLES
from finharness.tasks.subagent import default_specs
from finharness.tools.team_tools import default_teammate_specs


def test_auditor_is_one_role_with_two_deployments():
    sub = default_specs()["ledger_auditor"].system_prompt
    team = default_teammate_specs()["auditor_wang"].system_prompt
    duties = ROLES["auditor"].duties

    assert duties in sub and duties in team
    assert EPHEMERAL.strip() in sub
    assert PERSISTENT.strip() in team
    # 部署方式不能串台
    assert PERSISTENT.strip() not in sub
    assert EPHEMERAL.strip() not in team


def test_analyst_is_one_role_with_two_deployments():
    sub = default_specs()["finance_analyst"].system_prompt
    team = default_teammate_specs()["analyst_zhao"].system_prompt
    duties = ROLES["analyst"].duties

    assert duties in sub and duties in team
    assert EPHEMERAL.strip() in sub
    assert PERSISTENT.strip() in team


def test_auditor_has_no_write_permission_in_either_deployment():
    write_tools = {
        "submit_voucher", "withdraw_voucher", "post_voucher",
        "reject_voucher", "recategorize", "write_file",
    }
    assert default_specs()["ledger_auditor"].allow.isdisjoint(write_tools)
    assert default_teammate_specs()["auditor_wang"].allow.isdisjoint(write_tools)


def test_role_registry_matches_keys():
    for key, role in ROLES.items():
        assert role.key == key
