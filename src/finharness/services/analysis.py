"""多角色财务分析：四个维度并行，再汇总成体检报告。

## 相对原版的改动

原版用 LangGraph 的 StateGraph 编排：一个图，四个节点并行跑，最后一个节点汇总。
现在换成运行时的子 Agent 机制——**行为和产出形状保持不变**（`sections` + `report`），
但编排由自研运行时完成，不依赖任何框架。

## 为什么每个维度用独立的 harness

四个维度要真的并行（用线程池），而子 Agent 会共享主 Agent 的 hooks 与记忆实例——
并发跑会互相踩。所以每个维度**自己建一套 harness**：多花一点对象构造的开销，
换来彻底的状态隔离。财务分析这种几秒级的活，这点开销可以忽略。

## 指标先算、AI 后解读

`finance_analysis.all_metrics()` 是**纯计算，不调模型**，毫秒返回。
页面可以先把数字显示出来，AI 的解读随后补上——
用户不用盯着转圈等，体验差别很大。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ..assemble import build_harness
from . import finance_analysis

# 四个维度：名字 + 分析提纲
DIMENSIONS = (
    ("盈利能力",
     "分析这家企业的盈利能力：营收规模、费用结构、毛利率与净利率的趋势。"
     "先用 get_account_balance 和 query_entries 拿到数字，再给判断。"),
    ("偿债能力",
     "分析偿债能力：手头流动资金对短期负债的覆盖情况，有没有偿债压力。"
     "数字必须来自工具。"),
    ("营运能力",
     "分析营运效率：应收账款周转、存货周转、费用占比。数据不足就说数据不足。"),
    ("风险排查",
     "排查财务风险：大额异常支出、缺凭证的账目、疑似重复记账。"
     "调 reconcile_month 扫一遍，把发现按严重程度列出来。"),
)

SUMMARY_PROMPT = """下面是四个维度的专项分析：

{parts}

请汇总成一份财务体检报告，结构：
## 整体结论（一句话）
## 亮点（2-3 条）
## 风险与建议（2-3 条，每条要可执行）

控制在 400 字以内。"""


def _analyze_one(name: str, prompt: str) -> str:
    """跑一个维度的分析。每个维度独立 harness，避免并发共享状态。"""
    try:
        h = build_harness()
        rec = h["runner"].run(
            "finance_analyst",
            f"请分析这家企业【{name}】方面的情况。\n{prompt}\n"
            f"输出控制在 200 字以内，用要点。",
        )
        return rec.get("conclusion") or rec.get("error", "（无结论）")
    except Exception as e:  # noqa: BLE001 —— 单个维度失败不影响其它维度
        return f"（该维度分析失败：{type(e).__name__}: {e}）"


def run_analysis() -> dict:
    """四维度并行分析 + 汇总。

    会调 5 次大模型（4 个维度 + 1 个汇总），大约 30-60 秒——接口文档里要写明。
    """
    metrics = finance_analysis.all_metrics()

    with ThreadPoolExecutor(max_workers=len(DIMENSIONS)) as pool:
        futures = [pool.submit(_analyze_one, n, p) for n, p in DIMENSIONS]
        sections = {n: f.result() for (n, _), f in zip(DIMENSIONS, futures)}

    parts = "\n\n".join(f"【{n}】\n{c}" for n, c in sections.items())
    report = ""
    try:
        h = build_harness()
        report = h["llm"].complete(SUMMARY_PROMPT.format(parts=parts),
                                   system="你是财务分析师，只做汇总归纳，不引入新数据。")
    except Exception as e:  # noqa: BLE001
        report = f"（汇总失败：{e}）"

    return {"metrics": metrics, "sections": sections, "report": report.strip()}
