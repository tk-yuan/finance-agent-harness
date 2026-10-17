"""落盘文件必须读得出来。

实测踩过的坑：工具结果超过 2000 字符会落盘，而 read_file 默认一次读 4000 字符，
读出来的结果又超过阈值、再被落盘——模型于是"读文件 → 又落盘 → 再读"，
跑满 40 轮也拿不到内容。这两条测试锁住出口。
"""

import json

from finharness.context.compaction import SPILL_THRESHOLD
from finharness.context.memory import LayeredMemory
from finharness.runtime.hooks_impl import build_hooks
from finharness.runtime.loop import RunState
from finharness.runtime.permissions import PermissionPolicy
from finharness.runtime.tools import ToolRegistry
from finharness.tasks.todo import TaskBoard
from finharness.tools.builtin import register_fs_tools


def _registry(tmp_path):
    reg = ToolRegistry()
    register_fs_tools(reg, PermissionPolicy(allowed_roots=[tmp_path]))
    return reg


def test_read_file_pages_below_spill_threshold(tmp_path):
    text = "甲" * 5000
    (tmp_path / "big.json").write_text(text, encoding="utf-8")
    reg = _registry(tmp_path)

    out = reg.call("read_file", {"path": "big.json"})
    assert out["chars"] == 5000
    assert len(out["content"]) <= 1800
    assert out["next_offset"] == len(out["content"])
    # 关键：结果本身不能再超过落盘阈值
    assert len(json.dumps(out, ensure_ascii=False)) < SPILL_THRESHOLD

    rest = reg.call("read_file", {"path": "big.json", "offset": out["next_offset"]})
    assert rest["content"].startswith("甲")
    assert len(rest["content"]) <= 1800


def test_read_file_limit_is_clamped(tmp_path):
    (tmp_path / "big.json").write_text("乙" * 5000, encoding="utf-8")
    reg = _registry(tmp_path)

    out = reg.call("read_file", {"path": "big.json", "limit": 100000})
    assert len(out["content"]) <= 1800


def test_read_file_result_is_never_spilled(tmp_path):
    hooks = build_hooks(base_prompt="基础提示",
                        memory=LayeredMemory(tmp_path / "m.jsonl"),
                        board=TaskBoard())
    big = "丙" * 5000
    out = hooks.run("after_tool", {"name": "read_file", "result": big,
                                   "state": RunState(depth=0)})
    assert out.get("result") == big
