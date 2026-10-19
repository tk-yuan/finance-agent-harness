"""用项目二的评测集检验项目一的检索：向量 / 混合 / 混合+去重。

这是两个项目之间的闭环：项目二负责测出哪种方法好，这里验证搬过来之后
在项目一的索引上是否同样有效。

用法：
    python scripts/eval_retrieval.py                 # 默认 100 条，不重排
    python scripts/eval_retrieval.py --limit 400
    python scripts/eval_retrieval.py --rerank        # 加精排（慢，按调用计费）
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finharness.config import CACHE_DIR, DATA_DIR  # noqa: E402
from finharness.tools.retrieval import PolicyIndex  # noqa: E402

DEFAULT_EVALSET = Path(r"D:/Desktop/githup仓库/finance-rag-eval/data/processed/evalset.jsonl")
CONFIGS = (
    ("向量", "vector", False),
    ("向量+去重", "vector", True),
    ("混合", "hybrid", False),
    ("混合+去重", "hybrid", True),
)


def load_cases(path: Path, limit: int) -> list[dict]:
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows[:limit] if limit else rows


def evaluate(index: PolicyIndex, cases: list[dict], mode: str,
             dedupe: bool, use_rerank: bool, k: int) -> dict:
    hit1 = hit3 = 0
    t0 = time.perf_counter()
    for c in cases:
        hits = index.search(c["query"], k=k, recall=20, mode=mode,
                            dedupe_docs=dedupe, use_rerank=use_rerank)
        docs = [h["doc_id"] for h in hits]
        gold = c["gold_doc_id"]
        hit1 += bool(docs and docs[0] == gold)
        hit3 += gold in docs
    n = len(cases)
    return {
        "n": n,
        "recall1": hit1 / n if n else 0.0,
        "recall3": hit3 / n if n else 0.0,
        "ms_per_query": round((time.perf_counter() - t0) * 1000 / n, 1) if n else 0.0,
    }


def main() -> None:
    args = sys.argv[1:]
    limit = 100
    evalset = DEFAULT_EVALSET
    use_rerank = "--rerank" in args
    picked = [c for c in CONFIGS]
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
        if a == "--evalset" and i + 1 < len(args):
            evalset = Path(args[i + 1])
        if a == "--configs" and i + 1 < len(args):
            idxs = [int(x) - 1 for x in args[i + 1].split(",")]
            picked = [CONFIGS[j] for j in idxs if 0 <= j < len(CONFIGS)]

    if not evalset.is_file():
        raise SystemExit(f"找不到评测集：{evalset}")

    cases = load_cases(evalset, limit)
    index = PolicyIndex(DATA_DIR / "corpus.jsonl", CACHE_DIR / "kb_index")
    print(f"评测集 {evalset.name}：{len(cases)} 条 | 精排：{'开' if use_rerank else '关'}")
    print(f"索引片段：{index.build()} 条\n")

    print(f"{'配置':<12}{'Recall@1':>10}{'Recall@3':>10}{'ms/query':>12}")
    print("-" * 46)
    for name, mode, dedupe in picked:
        r = evaluate(index, cases, mode, dedupe, use_rerank, k=3)
        print(f"{name:<12}{r['recall1']:>9.1%}{r['recall3']:>10.1%}{r['ms_per_query']:>12.1f}")


if __name__ == "__main__":
    main()
