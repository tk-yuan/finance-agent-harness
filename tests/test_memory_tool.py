"""长期记忆「写入入口」的测试。

背景：`context/memory.py` 那套机制（衰减、强化、淘汰、召回）一直完整，
但**源码里没有任何地方调 `remember()`**——只有测试在调。
结果就是跑起来记忆库永远是空的。

这个文件锁的是补上的那个入口：模型自己判断该记什么，然后存下来。
"""

import io
import json

import pytest

from finharness.context.memory import LayeredMemory
from finharness.tools.memory_tools import register_memory_tools


@pytest.fixture
def mem(tmp_path):
    return LayeredMemory(path=tmp_path / "memory.jsonl")


@pytest.fixture
def remember(mem):
    from finharness.runtime.tools import ToolRegistry
    reg = ToolRegistry()
    register_memory_tools(reg, mem)
    return reg.get("remember").handler


# —— 基本写入 ——
def test_remember_writes_and_survives_reload(remember, mem, tmp_path):
    """记一条 → **换个新实例**还能召回。

    只断言"内存里有"是不够的——那证明不了它落盘了，重启就没了。
    """
    r = remember("房租要记销售费用，不是管理费用", tags="房租 科目")
    assert r["ok"] and r["id"]

    fresh = LayeredMemory(path=tmp_path / "memory.jsonl")   # 模拟"下次开新对话"
    hits = fresh.recall("房租 记 哪个 科目")
    assert hits, "新实例召不回，说明没落盘"
    assert "房租" in hits[0].content


def test_file_is_valid_jsonl(remember, tmp_path):
    remember("测试一条")
    lines = (tmp_path / "memory.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["content"] == "测试一条" and rec["kind"] == "pattern"


# —— 参数处理 ——
def test_empty_content_is_refused(remember):
    assert "error" in remember("   ")


def test_unknown_kind_falls_back_to_pattern(remember):
    """给了个不认识的类型不该报错，退化成默认——工具要皮实。"""
    r = remember("随便写个类型", kind="根本不存在的类型")
    assert r["ok"] and r["kind"] == "pattern"


def test_tags_accept_comma_or_space(remember):
    assert remember("逗号分隔", tags="房租,科目,费用")["tags"] == ["房租", "科目", "费用"]
    assert remember("空格分隔", tags="房租 科目 费用")["tags"] == ["房租", "科目", "费用"]


# —— 接线 ——
def test_tool_is_registered_and_writable():
    from finharness.assemble import build_harness
    t = build_harness()["registry"].get("remember")
    assert t is not None
    assert t.read_only is False, "它确实写文件，不能标成只读"
    assert not t.requires_approval, "记一条经验不该每次都弹审批——那没人会用"


def test_harness_memory_is_actually_wired():
    """装配出来的 harness 里，工具用的是**同一个** memory 对象——
    否则模型记的是一份、系统回想的是另一份，等于白记。

    这里只比对对象身份、不真的写：写了会落到仓库的 .cache/memory.jsonl 里污染环境。
    """
    from finharness.assemble import build_harness
    b = build_harness()
    tool = b["registry"].get("remember")
    captured = [c.cell_contents for c in (tool.handler.__closure__ or [])
                if isinstance(c.cell_contents, LayeredMemory)]
    assert captured, "工具没捕获到 memory 对象"
    assert captured[0] is b["memory"], "工具和 harness 用的不是同一个记忆库"
