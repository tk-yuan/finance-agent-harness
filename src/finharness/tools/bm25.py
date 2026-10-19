"""BM25 关键词检索与 RRF 融合。

与向量检索互补：向量擅长语义相近，BM25 擅长文号、数字、专有名词这类精确命中。
融合用 RRF——只看名次不看分数，所以余弦相似度和 BM25 分能安全合并。
"""

from __future__ import annotations

import math
import re
from collections import Counter

try:  # 有 jieba 用真分词；没有就退化，不给项目加硬依赖
    import jieba

    def tokenize(text: str) -> list[str]:
        return [w for w in jieba.lcut(text) if w.strip()]

except ImportError:

    def tokenize(text: str) -> list[str]:
        text = re.sub(r"\s+", "", text)
        chars = [c for c in text if c.strip()]
        if len(chars) < 2:
            return chars
        return [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]


class BM25:
    """Okapi BM25，内存索引。千级片段规模下无需倒排优化。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._docs: list[list[str]] = []
        self._tf: list[Counter] = []
        self._df: Counter = Counter()
        self._avgdl = 0.0

    def fit(self, corpus: list[str]) -> "BM25":
        self._docs = [tokenize(t) for t in corpus]
        self._tf = [Counter(d) for d in self._docs]
        self._df = Counter()
        for tf in self._tf:
            self._df.update(tf.keys())
        n = len(self._docs)
        self._avgdl = sum(len(d) for d in self._docs) / n if n else 0.0
        return self

    def _idf(self, term: str) -> float:
        n = len(self._docs)
        nt = self._df.get(term, 0)
        return math.log((n - nt + 0.5) / (nt + 0.5) + 1.0)

    def score(self, query: str, idx: int) -> float:
        tf = self._tf[idx]
        dl = len(self._docs[idx]) or 1
        s = 0.0
        for term in tokenize(query):
            f = tf.get(term, 0)
            if not f:
                continue
            s += self._idf(term) * f * (self.k1 + 1) / (
                f + self.k1 * (1 - self.b + self.b * dl / (self._avgdl or 1))
            )
        return s

    def search(self, query: str, top_k: int = 20) -> list[tuple[int, float]]:
        scored = [(i, self.score(query, i)) for i in range(len(self._docs))]
        scored = [x for x in scored if x[1] > 0]
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]


def rrf_fuse(rankings: list[list[int]], k: int = 60) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion：score(d) = Σ 1/(k + rank)。"""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: -kv[1])
