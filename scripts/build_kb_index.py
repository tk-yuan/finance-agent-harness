"""建立本地知识库索引（Agent 的检索工具用）。

用法：
    python scripts/build_kb_index.py

## 和姊妹项目的关系

语料与 `finance-rag-eval` 完全相同（同一份 549 份准则原文）。
那个项目负责**评测检索质量**，这里只需要**一个够用的检索**。

为了不再花一次 embedding 的钱，本脚本会**优先复用那边的嵌入缓存**
（`.cache/embed_cache.db`）。同一段文本的向量是确定的，直接搬过来即可——
所以第二次建索引几乎是瞬时的。
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finharness.config import CACHE_DIR, DATA_DIR  # noqa: E402
from finharness.tools.kb import kb_index  # noqa: E402

SISTER_CACHE = Path(r"D:\Desktop\githup仓库\finance-rag-eval\.cache\embed_cache.db")


def reuse_sister_cache() -> None:
    """复用姊妹项目的嵌入缓存，避免重复调用 embedding 接口。"""
    target = CACHE_DIR / "embed_cache.db"
    if target.exists() or not SISTER_CACHE.exists():
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SISTER_CACHE, target)
    print(f"  已复用姊妹项目的嵌入缓存（{SISTER_CACHE.stat().st_size / 1048576:.1f} MB）")


def main() -> None:
    corpus = DATA_DIR / "corpus.jsonl"
    if not corpus.exists():
        raise SystemExit(f"缺少语料：{corpus}")

    print("建立知识库索引 …")
    reuse_sister_cache()

    t0 = time.time()
    n = kb_index().build()
    print(f"  完成：{n} 个片段，用时 {time.time() - t0:.0f}s")

    # 冒烟：拿一个会计问题试检索
    print("\n检索冒烟：")
    for q in ("固定资产的折旧年限怎么确定", "办公用品应该计入什么科目"):
        hits = kb_index().search(q, k=2)
        print(f"  Q {q}")
        for h in hits:
            print(f"     → {h.get('doc_no') or h['doc_id']} | {h.get('title', '')[:44]}")


if __name__ == "__main__":
    main()
