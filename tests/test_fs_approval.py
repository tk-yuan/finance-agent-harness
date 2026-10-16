"""越界访问的授权测试。

原来的规则是"工作区外一律拒绝"。那条规则安全，但让 Agent 残废——
用户问"看看我桌面那个 Excel"，它只会回"路径越界"。

现在的规则是**"工作区免问，区外逐次要人点头"**——跟 Claude Code 一套模型。
这个文件锁的就是这条边界：**免问的地方别烦人，越界的地方一次都不能漏问。**
"""

import os
import threading
from pathlib import Path

import pytest

from finharness.config import WORKSPACE_DIR
from finharness.runtime.approval import ApprovalGate
from finharness.runtime.permissions import PermissionDenied, PermissionPolicy


def _gate(auto: bool):
    """造一个自动决策的门，并记录被问了什么。"""
    gate = ApprovalGate(timeout=5)
    asked = []

    def notify(req):
        asked.append(req)
        threading.Timer(0.05, lambda: gate.resolve(
            req.id, approved=auto, decider="测试")).start()

    gate.notifier = notify
    return gate, asked


INSIDE = str(WORKSPACE_DIR / "vouchers" / "README.txt")
OUTSIDE = "C:/Windows/win.ini" if os.name == "nt" else "/etc/hosts"


# —— 免问区 ——
def test_inside_workspace_is_never_asked():
    gate, asked = _gate(True)
    p = PermissionPolicy(gate=gate)
    p.check_path(INSIDE)
    p.check_path(INSIDE, write=True)
    assert asked == [], "工作区内不该弹任何授权请求"


def test_relative_path_still_lands_in_workspace():
    gate, asked = _gate(True)
    p = PermissionPolicy(gate=gate)
    resolved = p.check_path("reports/plan.md")
    assert resolved == (WORKSPACE_DIR / "reports" / "plan.md").resolve()
    assert asked == []


# —— 越界：必须问 ——
def test_outside_asks_then_allows():
    gate, asked = _gate(True)
    p = PermissionPolicy(gate=gate)
    assert p.check_path(OUTSIDE)              # 批准了就放行
    assert len(asked) == 1
    assert asked[0].kind == "approval"
    # 比较 resolve 后的路径：正斜杠/反斜杠、大小写在两个平台上写法不一样
    assert Path(str(asked[0].args.get("path", ""))).resolve() == Path(OUTSIDE).resolve()


def test_outside_asks_then_denies():
    gate, asked = _gate(False)
    p = PermissionPolicy(gate=gate)
    with pytest.raises(PermissionDenied):
        p.check_path(OUTSIDE)
    assert len(asked) == 1, "拒绝也必须问过——不能默默放行也不能默默拦下"


def test_write_outside_is_flagged_high_risk():
    """写比读危险：读错顶多看到不该看的内容，写错会毁掉人家的文件。"""
    gate, asked = _gate(True)
    PermissionPolicy(gate=gate).check_path(OUTSIDE, write=True)
    assert asked[0].risk == "high"


def test_read_outside_is_medium_risk():
    gate, asked = _gate(True)
    PermissionPolicy(gate=gate).check_path(OUTSIDE)
    assert asked[0].risk == "medium"


# —— 批过一次别重复烦人 ——
def test_grant_is_remembered_for_this_process():
    gate, asked = _gate(True)
    p = PermissionPolicy(gate=gate)
    for _ in range(4):
        p.check_path(OUTSIDE)
    assert len(asked) == 1, "同一个路径批一次就够了，不该每次都弹"


def test_two_different_paths_ask_twice():
    gate, asked = _gate(True)
    p = PermissionPolicy(gate=gate)
    p.check_path(OUTSIDE)
    p.check_path(OUTSIDE + ".bak")
    assert len(asked) == 2


# —— 没挂门时维持旧行为（CLI / 单测装配）——
def test_without_gate_outside_is_denied():
    """没有可交互的人，就不该有"问"这一步——直接拒，让模型知道此路不通。"""
    with pytest.raises(PermissionDenied):
        PermissionPolicy().check_path(OUTSIDE)


# —— 工具接线 ——
def test_run_command_is_registered_and_gated():
    from finharness.assemble import build_harness
    reg = build_harness()["registry"]
    t = reg.get("run_command")
    assert t is not None
    assert t.requires_approval, "执行命令必须每次都人工批准"
    assert not t.read_only
