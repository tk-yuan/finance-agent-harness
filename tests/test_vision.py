"""看图工具的测试。

**这些用例不打真模型**——CI 上不能有网络依赖，也不该为跑测试烧 token。
所以把 `ask_vision` 换成一个假的，只测**接线**：
路径解析对不对、权限过不过、格式拦不拦、空回答降不降级。

真模型那条路靠线上实测（用一张真发票跑通即可），不放进单测。
"""

import threading

import pytest

from finharness.config import DATA_DIR, WORKSPACE_DIR
from finharness.runtime.approval import ApprovalGate
from finharness.runtime.permissions import PermissionPolicy
from finharness.tools import vision as vision_mod


@pytest.fixture
def fake_vision(monkeypatch):
    """把视觉调用换成假的，并记录被问的问题。"""
    seen = {}

    def fake(path, prompt, max_tokens=600):
        seen["path"] = str(path)
        seen["prompt"] = prompt
        return "日期 2013-09-27，金额 3399.00，销售方 南京苏宁易购电子商务有限公司"

    monkeypatch.setattr(vision_mod, "ask_vision", fake)
    return seen


@pytest.fixture
def tool(fake_vision):
    from finharness.runtime.tools import ToolRegistry
    reg = ToolRegistry()
    vision_mod.register_vision_tools(reg, PermissionPolicy())
    return reg.get("read_image").handler


def _write_image(where):
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_bytes(b"\x89PNG\r\n\x1a\n")
    return where


# —— 路径解析：跨根找（这是踩过的坑）——
def test_relative_path_found_under_second_root(tool, fake_vision):
    """凭证在 data/attachments/，而第一个允许根是 workspace/。

    只按第一个根拼路径的话它会变成 workspace/data/attachments/…，永远找不到。
    """
    p = _write_image(DATA_DIR / "attachments" / "_t_vision.png")
    try:
        r = tool(path="attachments/_t_vision.png")
        assert "error" not in r, r
        assert p.resolve() == __import__("pathlib").Path(fake_vision["path"]).resolve()
    finally:
        p.unlink(missing_ok=True)


def test_workspace_relative_path_still_works(tool, fake_vision):
    p = _write_image(WORKSPACE_DIR / "artifacts" / "_t_vision2.png")
    try:
        r = tool(path="artifacts/_t_vision2.png")
        assert "error" not in r, r
    finally:
        p.unlink(missing_ok=True)


def test_absolute_path_works(tool, fake_vision):
    p = _write_image(DATA_DIR / "attachments" / "_t_vision3.png")
    try:
        assert "error" not in tool(path=str(p))
    finally:
        p.unlink(missing_ok=True)


# —— 格式与内容 ——
def test_rejects_unsupported_suffix(tool):
    p = DATA_DIR / "CORPUS_SOURCE.txt"          # 真实存在的非图片文件
    if not p.exists():
        pytest.skip("仓库里没有这个文件")
    r = tool(path=str(p))
    assert "error" in r and "格式" in r["error"], r


def test_missing_file_is_reported(tool):
    r = tool(path="attachments/_不存在_.png")
    assert "error" in r and "不存在" in r["error"]


def test_empty_model_answer_degrades(tool, monkeypatch):
    """模型没吐内容时要说清楚，而不是塞一个空答案让上层去猜。"""
    monkeypatch.setattr(vision_mod, "ask_vision", lambda *a, **k: "   ")
    p = _write_image(DATA_DIR / "attachments" / "_t_vision4.png")
    try:
        r = tool(path=str(p))
        assert "error" in r
    finally:
        p.unlink(missing_ok=True)


def test_custom_question_is_forwarded(tool, fake_vision):
    p = _write_image(DATA_DIR / "attachments" / "_t_vision5.png")
    try:
        tool(path=str(p), question="这张票的税号是多少？")
        assert fake_vision["prompt"] == "这张票的税号是多少？"
    finally:
        p.unlink(missing_ok=True)


# —— 权限：看图≠能看你电脑上所有的图 ——
def test_outside_image_is_denied_without_gate(tool):
    """没挂门时区外一律拒（CLI / 单测装配）。用临时目录，避免写死盘符。"""
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp(prefix="vision_deny_"))
    try:
        img = _write_image(tmp / "outside.png")
        r = tool(path=str(img))
        assert "error" in r and "允许" in r["error"], r
    finally:
        __import__("shutil").rmtree(tmp, ignore_errors=True)


def test_outside_image_asks_and_allows(monkeypatch, fake_vision):
    """区外的图也要人点允许——和 read_file 走同一套授权。"""
    import tempfile
    from pathlib import Path
    from finharness.runtime.tools import ToolRegistry

    gate = ApprovalGate(timeout=5)
    asked = []
    gate.notifier = lambda req: (asked.append(req),
                                 threading.Timer(0.05, lambda: gate.resolve(
                                     req.id, approved=True, decider="测试")).start())

    reg = ToolRegistry()
    vision_mod.register_vision_tools(reg, PermissionPolicy(gate=gate))

    # 用系统临时目录：两个平台上都是绝对路径、且都在允许根之外。
    # 别写 C:/Windows/… 那种路径——Linux 上它不是绝对路径，测试会假过。
    tmp = Path(tempfile.mkdtemp(prefix="vision_test_"))
    try:
        img = _write_image(tmp / "outside.png")
        r = reg.get("read_image").handler(path=str(img))
        assert len(asked) == 1, "区外的图必须问过"
        assert "error" not in r, r
    finally:
        __import__("shutil").rmtree(tmp, ignore_errors=True)


def test_read_image_is_registered():
    from finharness.assemble import build_harness
    assert "read_image" in build_harness()["registry"].names()
