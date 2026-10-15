"""内控审计：把"不该被允许的操作"全试一遍，看拦不拦得住。

## 为什么单独做一份

单元测试证明的是"某个函数返回值对不对"；
审计要回答的是**业务问题**：一个会动钱的 Agent，边界到底守没守住。

这两件事不重合。比如"审批门被拒时工具到底执不执行"，
单测里各部件都通过，但连起来可能照样把账记了。

## 口径

每一条都是一次**真实调用**（走工具层，不是读代码判断），断言它被拒绝。
拒绝 = 返回 error / 或审批未通过导致工具未执行。
被拒绝的条数 / 总条数 = 拦截率。

用法：
    python scripts/audit_controls.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Windows 控制台默认是 GBK，打不出 ✓/✗ 会直接 UnicodeEncodeError 崩掉。
# 审计脚本要在本机和 Linux CI 上都能跑，所以显式把输出钉成 UTF-8。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 老版本 Python 或已被重定向
        pass

# 必须在 import finharness 之前把库指到临时副本——审计会写数据，不能污染演示账套
_TMP = Path(tempfile.mkdtemp(prefix="fin_audit_"))
_DB = _TMP / "audit.db"
_shutil_src = ROOT / "data" / "finance.db"
if _shutil_src.is_file():
    shutil.copy2(_shutil_src, _DB)
os.environ["FIN_DB_PATH"] = str(_DB)

sys.path.insert(0, str(ROOT / "src"))

from finharness.domain.db._session import session, init_db  # noqa: E402
from finharness.domain.db.models import LedgerEntry  # noqa: E402
from finharness.domain.db.models import ClosedPeriod  # noqa: E402
from finharness.runtime.approval import ApprovalGate, ApprovalRequest  # noqa: E402
from finharness.tools import finance as F  # noqa: E402
from finharness.tools import ledger as L  # noqa: E402

init_db()

# 审计用凭证：真实建一个文件，好让"金额/方向"那几条能走到后面的校验。
# 必须建在 data/attachments/ 下（submit_voucher 只认这个目录），所以跑完要删掉——
# 否则会在仓库里留下一个测试残留文件。
L.ATTACH_DIR.mkdir(parents=True, exist_ok=True)
_VOUCHER = L.ATTACH_DIR / "_audit_voucher.png"
_VOUCHER.write_bytes(b"\x89PNG\r\n\x1a\n")


def _seed_pending(period: str = "2026-10") -> int:
    """造一张待审核单据，返回 id。用来测"待审核非空不许结账"。"""
    with session() as db:
        row = LedgerEntry(date=f"{period}-05", amount=100.0, description="审计用单据",
                          category="", entry_type="expense", debit_account="",
                          credit_account="", attachment=_VOUCHER.name,
                          payment="银行存款", status="pending")
        db.add(row)
        db.flush()
        return row.id


def _approved_id() -> int:
    with session() as db:
        row = db.query(LedgerEntry).filter(LedgerEntry.status == "approved").first()
        return row.id if row else -1


def _closed_period() -> str:
    with session() as db:
        row = db.query(ClosedPeriod).first()
        return row.period if row else ""


def blocked(result) -> bool:
    """工具返回 {'error': ...} 即为拦住。"""
    return isinstance(result, dict) and "error" in result


# —— 审计项 ——

def s_no_voucher():
    return L.submit_voucher(amount=100, description="买打印纸", voucher="")


def s_voucher_missing():
    return L.submit_voucher(amount=100, description="买打印纸", voucher="__不存在__.png")


def s_zero_amount():
    return L.submit_voucher(amount=0, description="买打印纸", voucher=_VOUCHER.name)


def s_negative_amount():
    return L.submit_voucher(amount=-500, description="买打印纸", voucher=_VOUCHER.name)


def s_bad_direction():
    return L.submit_voucher(amount=100, description="买打印纸", voucher=_VOUCHER.name,
                            entry_type="transfer")


def s_unknown_account():
    pid = _seed_pending()
    return L.post_voucher(entry_id=pid, category="随便编的科目")


def s_double_posting():
    aid = _approved_id()
    return L.post_voucher(entry_id=aid, category="办公费")


def s_withdraw_approved():
    aid = _approved_id()
    return L.withdraw_voucher(entry_id=aid, reason="想撤")


def s_reject_without_reason():
    pid = _seed_pending()
    return L.reject_voucher(entry_id=pid, reason="   ")


def s_close_again():
    p = _closed_period()
    if not p:
        raise RuntimeError("演示账套里没有已结账期间，无法审计这一条")
    return F.close_period(period=p)


def s_close_with_pending():
    _seed_pending("2027-03")          # 一个没结过账的空期间 + 一张待审核
    return F.close_period(period="2027-03")


def s_write_outside_whitelist():
    """虚拟文件系统的路径白名单：写到允许根目录之外必须被拒。

    write_file 是 register_fs_tools 里的闭包（要绑定 permission policy），
    所以从工具池里取，而不是直接 import。
    """
    from finharness.assemble import build_harness

    bundle = build_harness()
    tool = bundle["registry"].get("write_file")
    return tool.handler(path="C:/Windows/System32/evil.txt", content="x")


def s_approval_rejected_blocks_execution():
    """审批门拒绝时，高风险工具**不能执行**。

    走运行时的真实路径：装配 harness → 用拒绝门 → 调 post_voucher。
    """
    from finharness.assemble import build_harness

    gate = ApprovalGate(timeout=5)

    def reject(req: ApprovalRequest) -> None:
        gate.resolve(req.id, approved=False, decider="audit", note="审计：一律拒绝")

    gate.notifier = reject
    bundle = build_harness(approval_gate=gate)
    agent = bundle["agent"]

    pid = _seed_pending("2027-06")
    text = agent._run_tool({
        "id": "audit-call",
        "function": {"name": "post_voucher",
                     "arguments": f'{{"entry_id": {pid}, "category": "办公费"}}'},
    })
    # 被拦的标志：返回文本是拒绝提示，而不是成功 JSON
    ok = ("被拒绝" in text) or ("未通过" in text)
    return {"ok": ok, "detail": text[:120]}


SCENARIOS = [
    ("无凭证记账（未提供凭证）", s_no_voucher),
    ("无凭证记账（凭证文件不存在）", s_voucher_missing),
    ("金额为 0", s_zero_amount),
    ("金额为负数", s_negative_amount),
    ("非法收付方向", s_bad_direction),
    ("入账用科目表外的科目", s_unknown_account),
    ("对已入账单据重复入账", s_double_posting),
    ("撤回已入账单据", s_withdraw_approved),
    ("驳回不写理由", s_reject_without_reason),
    ("对已结账期间再结账", s_close_again),
    ("待审核队列非空时结账", s_close_with_pending),
    ("写入白名单之外的路径", s_write_outside_whitelist),
]


def main() -> None:
    try:
        _run()
    finally:
        # 凭证建在仓库的 data/attachments/ 下，跑完（哪怕中途报错）都必须删掉
        _VOUCHER.unlink(missing_ok=True)
        shutil.rmtree(_TMP, ignore_errors=True)


def _run() -> None:
    print("=" * 64)
    print("内控审计：逐条尝试违规操作，看系统拦不拦得住")
    print("=" * 64)
    print(f"临时账套：{_DB}（演示库的副本，审计不会污染真实数据）\n")

    passed, failed = 0, []
    for name, fn in SCENARIOS:
        try:
            r = fn()
            ok = blocked(r)
            detail = r.get("error", "") if isinstance(r, dict) else str(r)[:80]
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"抛出异常 {type(e).__name__}: {e}"

        mark = "✓ 拦住" if ok else "✗ 未拦住"
        print(f"  {mark}  {name}")
        if detail:
            print(f"           → {str(detail)[:74]}")
        passed += ok
        if not ok:
            failed.append(name)

    # 审批门单独一条：它拦的是"执行"，不是"返回值"
    print()
    name = "审批被拒时高风险工具不执行"
    try:
        r = s_approval_rejected_blocks_execution()
        ok = bool(r.get("ok"))
        print(f"  {'✓ 拦住' if ok else '✗ 未拦住'}  {name}")
        print(f"           → {r.get('detail', '')[:74]}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  ✗ 未拦住  {name}")
        print(f"           → 抛出异常 {type(e).__name__}: {e}")
    passed += ok
    if not ok:
        failed.append(name)

    total = len(SCENARIOS) + 1
    print()
    print("=" * 64)
    print(f"内控拦截率   {passed}/{total}  =  {passed / total:.0%}")
    print("=" * 64)
    if failed:
        print("未拦住的：")
        for f in failed:
            print(f"  · {f}")

    raise SystemExit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
