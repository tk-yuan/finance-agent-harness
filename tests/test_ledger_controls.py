"""财务域的内控约束测试。

这个项目区别于"通用 Agent 框架"的地方，就在这些约束上：
借贷方向、凭证前置、审核状态机——都是**代码级**的硬约束，
不是写在提示词里指望模型自觉。

提示词会被模型忽略，代码不会。
"""

import pytest

from finharness.tools.ledger import ATTACH_DIR, make_entries, submit_voucher


@pytest.fixture
def voucher():
    """在凭证目录里放一张临时凭证，测完删掉。

    不让测试依赖仓库里恰好存在某个图片文件——CI 是干净克隆，
    "本机碰巧有"的文件在那边就是不存在。
    """
    ATTACH_DIR.mkdir(parents=True, exist_ok=True)
    p = ATTACH_DIR / "_test_voucher.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    yield p.name
    p.unlink(missing_ok=True)


# —— 复式记账：借贷方向不能交给 LLM ——

def test_expense_debits_the_category():
    assert make_entries("expense", "房租", "银行存款") == ("房租", "银行存款")


def test_income_credits_the_category():
    assert make_entries("income", "主营业务收入", "银行存款") == ("银行存款", "主营业务收入")


def test_asset_purchase_does_not_hit_profit_and_loss():
    """买设备是资产内部转换，不能进当期损益——这一步错了报表就废了。"""
    debit, credit = make_entries("asset", "固定资产", "银行存款")
    assert debit == "固定资产"
    assert credit == "银行存款"


def test_unknown_direction_raises():
    with pytest.raises(ValueError):
        make_entries("transfer", "房租", "银行存款")


# —— 凭证前置：没有票据就不许记账 ——

def test_submit_without_voucher_is_refused():
    r = submit_voucher(amount=100, description="买打印纸", voucher="")
    assert "error" in r
    assert "凭证" in r["error"]


def test_submit_with_missing_attachment_is_refused():
    r = submit_voucher(amount=100, description="买打印纸", voucher="不存在的文件.png")
    assert "error" in r
    assert "找不到凭证" in r["error"]


def test_non_positive_amount_is_refused(voucher):
    r = submit_voucher(amount=0, description="买打印纸", voucher=voucher)
    assert "error" in r
    assert "大于 0" in r["error"]


def test_invalid_direction_is_refused(voucher):
    r = submit_voucher(amount=100, description="买打印纸", voucher=voucher,
                       entry_type="transfer")
    assert "error" in r
    assert "收付方向" in r["error"]
