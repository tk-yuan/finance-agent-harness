"""会计科目判定准确率评测。

## 为什么要有这个数字

这个项目的机制（审批门、上下文压缩、Agent Teams）能被单元测试证明"没坏"，
但证明不了"有用"。**有用**要拿业务数字说话——就像税筹版那句"节税 66.7 万元"。

本脚本给出的就是这样一个数字：**给定真实业务单据，系统判科目判得准不准。**

## 数据从哪来

`--ledger` 指向一份真实账套（548 条已入账单据，跨 12 个月、19 个科目）。
评测时把 `category` 遮住当金标，只把**收付方向 + 摘要 + 金额 + 支付账户**喂给系统。

金标的成色要说清楚：这份账套是照着《小企业会计准则》的业务场景构造的，
不是注册会计师逐条标注的。其中 73 条摘要里有 2 条对应多个科目，已排除。
口径写在这里，不藏在结论后面。

## 对照基线

只给系统打分是不够的——**"85% 准确率"本身说明不了好坏**。
所以同时跑一个关键词规则基线（摘要里出现"房租"就判房租，等等），
它代表"不引入模型、纯规则"的朴素做法。两个数放一起才有意义。

用法：
    python scripts/eval_account_mapping.py                # 用仓库自带的评测账套
    python scripts/eval_account_mapping.py 30             # 每个科目最多抽 30 条
    python scripts/eval_account_mapping.py --ledger D:/path/to/ledger.db

默认账套 `data/eval_ledger.db` 随仓库提供（548 条已入账单据），克隆下来不用另找数据。

**为什么评测不直接用运行时的 `data/finance.db`**：那是活库，界面上的记账、结账都会改它。
拿它做评测，数字就会随着"你玩了几次演示"而漂移，README 里那组结果也就不再可复现。
内容上两者现在是同一份账套，只是角色不同——一个给人用，一个给数字用。
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finharness.domain import accounts as A  # noqa: E402
from finharness.llm import LLMClient  # noqa: E402

DEFAULT_LEDGER = ROOT / "data" / "eval_ledger.db"
REPORT_DIR = ROOT / "reports"

# 可选科目白名单：与记账工具的校验口径完全一致，避免"判对了但系统不认"
ALLOWED = A.EXPENSE_ACCOUNTS + A.INCOME_ACCOUNTS + ["固定资产"]

SYSTEM = (
    "你是一家小微企业的会计。给你一张已发生的业务单据，你要判定它应当计入哪个会计科目。\n"
    "只能从给定科目表里选一个，不要自造科目名，也不要在答案里写别的字。\n\n"
    "科目表：\n" + "、".join(ALLOWED) + "\n\n"
    "判断依据：\n"
    "- 收付方向是 expense 表示付钱出去，通常进费用类科目；income 表示收钱进来，通常进收入类科目\n"
    "- 支付/收款账户只说明钱从哪个户走，不决定科目\n"
    "- 摘要里的业务实质决定科目，例如「买办公用品」进办公费、「付房租」进房租\n"
)

# —— 关键词基线 ——
# 刻意写得像"一个懂点会计的人会写的规则"，而不是稻草人：
# 按业务关键词从具体到宽泛排序，命中即返回。
KEYWORD_RULES: list[tuple[str, str]] = [
    ("销售商品", "主营业务收入"), ("提供服务", "主营业务收入"), ("收入", "主营业务收入"),
    ("采购商品", "主营业务成本"), ("结转", "主营业务成本"),
    ("社保", "社保费"), ("公积金", "社保费"),
    ("工资", "工资"), ("薪酬", "工资"),
    ("房租", "房租"), ("租金", "房租"),
    ("水电", "水电费"), ("电费", "水电费"), ("水费", "水电费"),
    ("办公", "办公费"), ("文具", "办公费"), ("耗材", "办公费"),
    ("差旅", "差旅费"), ("住宿", "差旅费"), ("机票", "差旅费"),
    ("招待", "业务招待费"), ("餐费", "业务招待费"), ("宴请", "业务招待费"),
    ("通讯", "通讯费"), ("电话", "通讯费"), ("宽带", "通讯费"), ("域名", "通讯费"),
    ("交通", "交通费"), ("打车", "交通费"), ("滴滴", "交通费"), ("油费", "交通费"),
    ("手续费", "银行手续费"), ("结息", "银行手续费"), ("利息", "银行手续费"),
    ("税", "税费"), ("发票", "税费"),
    ("折旧", "折旧费"),
    ("备用金", "库存现金"), ("现金", "库存现金"),
]


def baseline_predict(description: str, entry_type: str) -> str:
    """关键词基线：摘要命中哪个词就判哪个科目，都不命中就退到方向默认值。"""
    for kw, acc in KEYWORD_RULES:
        if kw in description:
            return acc
    return "主营业务收入" if entry_type == "income" else "其他"


def load_cases(ledger: Path, cap_per_category: int, seed: int = 42) -> tuple[list[dict], dict]:
    """从真实账套里抽评测样本。

    **为什么要按类别均衡**：这份账套里 260/460 条都是「销售商品-某公司 → 主营业务收入」，
    同一个判断重复了几百遍。直接在全量上跑，准确率会被这种同质条目抬高，
    看起来漂亮但说明不了问题。所以每个科目最多抽 cap_per_category 条，
    让 14 个科目都有份——**这才测得出"科目判得准不准"**。
    """
    con = sqlite3.connect(str(ledger))
    rows = con.execute(
        "select description, amount, category, entry_type, payment from ledger_entries"
    ).fetchall()
    con.close()

    # 摘要 → 科目集合，用于剔除"同一摘要对应多个科目"的歧义样本
    by_desc: dict[str, set[str]] = defaultdict(set)
    for desc, _amt, cat, _et, _pay in rows:
        by_desc[desc].add(cat)
    ambiguous = {d for d, cats in by_desc.items() if len(cats) > 1}

    stats = {
        "ledger_rows": len(rows),
        "ambiguous_descriptions": sorted(ambiguous),
        "excluded_ambiguous": 0,
        "excluded_direction": 0,
        "excluded_unknown_account": 0,
    }

    pool = []
    for desc, amt, cat, et, pay in rows:
        if desc in ambiguous:
            stats["excluded_ambiguous"] += 1
            continue
        # 只测系统真正支持的收付方向（submit_voucher 只认 expense/income/asset）
        if et not in ("expense", "income", "asset"):
            stats["excluded_direction"] += 1
            continue
        # 金标不在系统科目表里的无法判定对错，直接排除并报数
        if cat not in ALLOWED:
            stats["excluded_unknown_account"] += 1
            continue
        pool.append({"description": desc, "amount": amt, "gold": cat,
                     "entry_type": et, "payment": pay})

    rng = random.Random(seed)
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for c in pool:
        by_cat[c["gold"]].append(c)

    sample: list[dict] = []
    per_cat: dict[str, int] = {}
    for cat, items in sorted(by_cat.items()):
        rng.shuffle(items)
        take = items[:cap_per_category]
        sample.extend(take)
        per_cat[cat] = len(take)

    stats["pool_size"] = len(pool)
    stats["distinct_descriptions"] = len({c["description"] for c in pool})
    stats["sampled"] = len(sample)
    stats["sampled_distinct_descriptions"] = len({c["description"] for c in sample})
    stats["per_gold_sampled"] = per_cat
    stats["cap_per_category"] = cap_per_category
    return sample, stats


def build_prompt(case: dict) -> str:
    direction = {"expense": "付钱出去（费用）", "income": "收钱进来（收入）",
                 "asset": "资产购置"}.get(case["entry_type"], case["entry_type"])
    return (
        f"业务单据：\n"
        f"- 摘要：{case['description']}\n"
        f"- 金额：{case['amount']:,.2f} 元\n"
        f"- 收付方向：{direction}\n"
        f"- 账户：{case['payment']}\n\n"
        f"请只回答一个科目名。"
    )


def parse_account(text: str) -> str:
    """从模型输出里抠出科目名：优先精确匹配，其次取白名单里出现的最长项。"""
    t = (text or "").strip().strip("。.、,，\"'`")
    if t in ALLOWED:
        return t
    hits = [a for a in ALLOWED if a in t]
    if hits:
        return max(hits, key=len)
    return t[:20] if t else "(空)"


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    cap = int(args[0]) if args else 15
    ledger = DEFAULT_LEDGER
    for a in sys.argv[1:]:
        if a.startswith("--ledger="):
            ledger = Path(a.split("=", 1)[1])

    if not ledger.is_file():
        raise SystemExit(f"找不到账套：{ledger}")

    cases, stats = load_cases(ledger, cap)
    print(f"评测集：账套 {stats['ledger_rows']} 条账目 → 合格 {stats['pool_size']} 条"
          f"（{stats['distinct_descriptions']} 类摘要）")
    print(f"  排除：摘要歧义 {stats['excluded_ambiguous']} 条"
          f"（{'、'.join(stats['ambiguous_descriptions']) or '无'}）"
          f" / 方向不支持 {stats['excluded_direction']} 条"
          f" / 金标不在科目表 {stats['excluded_unknown_account']} 条")
    print(f"  抽样：每个科目最多 {cap} 条 → 实抽 {stats['sampled']} 条"
          f"（覆盖 {len(stats['per_gold_sampled'])} 个科目、"
          f"{stats['sampled_distinct_descriptions']} 类摘要）")
    print()

    llm = LLMClient()

    # —— 基线先跑（不花钱）——
    base_hits = sum(1 for c in cases if baseline_predict(c["description"], c["entry_type"]) == c["gold"])

    # —— 系统判定 ——
    t0 = time.time()
    sys_hits = 0
    errors: list[dict] = []
    conf: Counter = Counter()
    per_gold: dict[str, list[bool]] = defaultdict(list)
    per_case: list[dict] = []

    for i, c in enumerate(cases, 1):
        try:
            out = llm.complete(build_prompt(c), system=SYSTEM, max_tokens=32)
            pred = parse_account(out)
        except Exception as e:  # noqa: BLE001
            pred = f"(调用失败: {type(e).__name__})"
        ok = pred == c["gold"]
        sys_hits += ok
        per_gold[c["gold"]].append(ok)
        per_case.append({"description": c["description"], "gold": c["gold"],
                         "pred": pred, "ok": ok})
        if not ok:
            errors.append({"description": c["description"], "gold": c["gold"],
                           "pred": pred, "entry_type": c["entry_type"]})
            conf[(c["gold"], pred)] += 1
        if i % 20 == 0 or i == len(cases):
            print(f"  进度 {i}/{len(cases)}  当前准确率 {sys_hits / i:.1%}")
    wall = time.time() - t0

    total = len(cases)
    sys_acc = sys_hits / total
    base_acc = base_hits / total

    # 同一摘要可能出现多次，实例级准确率会被高频摘要主导——
    # 所以再按「摘要」去重算一次：每个摘要只要有一次判错，就算这类没拿下
    desc_stat: dict[str, list[bool]] = defaultdict(list)
    for r in per_case:
        desc_stat[r["description"]].append(r["ok"])
    desc_ok = sum(1 for v in desc_stat.values() if all(v))
    desc_acc = desc_ok / len(desc_stat)

    print()
    print("=" * 60)
    print(f"系统判定准确率   {sys_acc:.1%}  ({sys_hits}/{total} 张单据)")
    print(f"  按摘要去重     {desc_acc:.1%}  ({desc_ok}/{len(desc_stat)} 类业务摘要)")
    print(f"关键词基线       {base_acc:.1%}  ({base_hits}/{total})")
    print(f"提升            +{(sys_acc - base_acc) * 100:.1f} 个百分点")
    print("=" * 60)
    print(f"耗时 {wall:.0f}s | 模型调用 {llm.usage['calls']} 次 "
          f"| token {llm.usage['total_tokens']:,}")

    report = {
        "eval": "account_mapping",
        "ledger": str(ledger),
        "n": total,
        "seed": 42,
        "model": llm.model,
        "system_accuracy": round(sys_acc, 4),
        "system_accuracy_by_description": round(desc_acc, 4),
        "baseline_accuracy": round(base_acc, 4),
        "delta_pp": round((sys_acc - base_acc) * 100, 2),
        "by_gold": {k: {"n": len(v), "acc": round(sum(v) / len(v), 3)}
                    for k, v in sorted(per_gold.items(), key=lambda kv: -len(kv[1]))},
        "confusions": [{"gold": g, "pred": p, "n": c}
                       for (g, p), c in conf.most_common(12)],
        "errors": errors[:40],
        "per_case": per_case,
        "stats": stats,
        "usage": llm.usage,
        "wall_sec": round(wall, 1),
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "account_mapping_eval.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细 -> reports/account_mapping_eval.json")

    # 错在哪，比错了多少更重要
    if errors:
        print("\n判错的样本：")
        for e in errors[:10]:
            print(f"  {e['description'][:30]}  金标 {e['gold']} → 判成 {e['pred']}")
    else:
        print("\n本轮没有判错的样本。")


if __name__ == "__main__":
    main()
