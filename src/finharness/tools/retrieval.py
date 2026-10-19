"""精简政策检索：给 Agent 用的检索工具。

与独立项目 `finance-rag-eval` 的分工：
  那个项目专门做**检索质量评测**——切分策略对比、消融实验、量化指标；
  本项目只需要一个**够用的检索**，但已把那边验证有效的两项搬了过来：
  混合召回（向量 + BM25，RRF 融合）与文档级去重。

检索内核仍然是自己写的：
  · 切分：按条款边界切（政策文本天然有「一、」「二、」结构）
  · 向量：bge-m3（硅基流动），numpy 精确余弦检索
  · 关键词：手写 BM25（文号、数字这类精确命中）
  · 融合：RRF，只看名次不看分数
  · 精排：bge-reranker-v2-m3
"""

from __future__ import annotations

import json
import re
import sqlite3
import struct
import hashlib
import time
from pathlib import Path

import httpx
import numpy as np

from ..config import CACHE_DIR, EMBED_MODEL, SILICONFLOW_API_KEY, SILICONFLOW_BASE_URL
from .bm25 import BM25, rrf_fuse

ARTICLE_SPLIT = re.compile(
    r"(?m)(?=^\s*(?:第[一二三四五六七八九十百零]+\s*条|[一二三四五六七八九十]+\s*、))"
)
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


# —— 嵌入（带磁盘缓存，同一段文本只付一次钱）——
class _Cache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS emb (k TEXT PRIMARY KEY, dim INT, v BLOB)")
        self.conn.commit()

    @staticmethod
    def key(model: str, text: str) -> str:
        return hashlib.sha1(f"{model}\x00{text}".encode()).hexdigest()

    def get_many(self, keys: list[str]) -> dict[str, list[float]]:
        out = {}
        for i in range(0, len(keys), 500):
            blk = keys[i : i + 500]
            q = ",".join("?" * len(blk))
            for k, dim, v in self.conn.execute(f"SELECT k,dim,v FROM emb WHERE k IN ({q})", blk):
                out[k] = list(struct.unpack(f"<{dim}f", v))
        return out

    def put_many(self, items: list[tuple[str, list[float]]]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO emb (k,dim,v) VALUES (?,?,?)",
            [(k, len(v), struct.pack(f"<{len(v)}f", *v)) for k, v in items],
        )
        self.conn.commit()


def _post(path: str, payload: dict, retries: int = 4) -> dict:
    url = SILICONFLOW_BASE_URL.rstrip("/") + path
    headers = {"Authorization": f"Bearer {SILICONFLOW_API_KEY}", "Content-Type": "application/json"}
    last = None
    for i in range(retries):
        try:
            r = httpx.post(url, headers=headers, json=payload, timeout=60.0)
            if r.status_code == 200:
                return r.json()
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:100]}")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5**i)
    raise RuntimeError(f"请求失败：{last}")


def embed_texts(texts: list[str], model: str = EMBED_MODEL, batch: int = 16,
                cache: _Cache | None = None) -> list[list[float]]:
    texts = [t if t.strip() else " " for t in texts]
    keys = [_Cache.key(model, t) for t in texts]
    found = cache.get_many(keys) if cache else {}
    todo = [(i, t, k) for i, (t, k) in enumerate(zip(texts, keys)) if k not in found]
    for i in range(0, len(todo), batch):
        blk = todo[i : i + batch]
        data = _post("/embeddings", {"model": model, "input": [t for _, t, _ in blk]})
        pairs = []
        for (_, _, k), item in zip(blk, data["data"]):
            found[k] = item["embedding"]
            pairs.append((k, item["embedding"]))
        if cache:
            cache.put_many(pairs)
    return [found[k] for k in keys]


def rerank(query: str, docs: list[str], top_n: int) -> list[tuple[int, float]]:
    data = _post(
        "/rerank",
        {"model": RERANK_MODEL, "query": query, "documents": docs, "top_n": top_n},
    )
    return [(r["index"], float(r["relevance_score"])) for r in data["results"]]


def split_by_article(text: str, max_chars: int = 800) -> list[str]:
    out = []
    for part in ARTICLE_SPLIT.split(text):
        part = part.strip()
        if not part:
            continue
        if len(part) <= max_chars:
            out.append(part)
        else:
            for i in range(0, len(part), max_chars - 100):
                out.append(part[i : i + max_chars])
    return out


class PolicyIndex:
    """政策索引：构建一次，之后从磁盘加载。"""

    def __init__(self, corpus_path: Path, index_dir: Path) -> None:
        self.corpus_path = Path(corpus_path)
        self.index_dir = Path(index_dir)
        self.chunks: list[dict] = []
        self.vectors: np.ndarray | None = None
        self.cache = _Cache(CACHE_DIR / "embed_cache.db")
        self._bm25: BM25 | None = None

    # —— 构建 ——
    def build(self, rebuild: bool = False) -> int:
        meta_p, vec_p = self.index_dir / "meta.json", self.index_dir / "vectors.npy"
        if not rebuild and meta_p.exists() and vec_p.exists():
            self._load()
            return len(self.chunks)

        docs = [json.loads(l) for l in self.corpus_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        chunks = []
        for d in docs:
            for j, text in enumerate(split_by_article(d["text"])):
                chunks.append(
                    {
                        "chunk_id": f"{d['doc_id']}:{j:04d}",
                        "doc_id": d["doc_id"],
                        "text": text,
                        "title": d.get("title", ""),
                        "doc_no": d.get("doc_no", ""),
                        "issued": d.get("issued", ""),
                        "valid_to": d.get("valid_to", ""),
                    }
                )
        vecs = np.asarray(embed_texts([c["text"] for c in chunks], cache=self.cache), dtype=np.float32)
        vecs /= np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9)

        self.index_dir.mkdir(parents=True, exist_ok=True)
        meta_p.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
        np.save(vec_p, vecs)
        self.chunks, self.vectors = chunks, vecs
        return len(chunks)

    def _load(self) -> None:
        self.chunks = json.loads((self.index_dir / "meta.json").read_text(encoding="utf-8"))
        self.vectors = np.load(self.index_dir / "vectors.npy")

    def ensure(self) -> None:
        if not self.chunks:
            self.build()

    # —— 检索：混合召回 + 去重 + 精排 ——
    def _ensure_bm25(self) -> BM25:
        if self._bm25 is None:                       # 首次用时才建，纯内存
            self._bm25 = BM25().fit([c["text"] for c in self.chunks])
        return self._bm25

    def _vector_recall(self, query: str, n: int) -> list[int]:
        qv = np.asarray(embed_texts([query], cache=self.cache)[0], dtype=np.float32)
        qv /= max(float(np.linalg.norm(qv)), 1e-9)
        sims = self.vectors @ qv
        n = min(n, len(sims))
        idx = np.argpartition(-sims, n - 1)[:n]
        return [int(i) for i in idx[np.argsort(-sims[idx])]]

    def _bm25_recall(self, query: str, n: int) -> list[int]:
        return [i for i, _ in self._ensure_bm25().search(query, top_k=n)]

    def _dedupe_by_doc(self, idxs: list[int], limit: int) -> list[int]:
        """同一份文件只留排名最高的片段。doc 级召回任务里，同源片段会白占名额。"""
        seen: set[str] = set()
        out: list[int] = []
        for i in idxs:
            did = self.chunks[i].get("doc_id")
            if did in seen:
                continue
            seen.add(did)
            out.append(i)
            if len(out) >= limit:
                break
        return out

    def search(self, query: str, k: int = 3, recall: int = 20,
               mode: str = "hybrid", dedupe_docs: bool = True,
               use_rerank: bool = True) -> list[dict]:
        self.ensure()
        if mode == "vector":
            rankings = [self._vector_recall(query, recall)]
        elif mode == "bm25":
            rankings = [self._bm25_recall(query, recall)]
        elif mode == "hybrid":
            rankings = [self._vector_recall(query, recall),
                        self._bm25_recall(query, recall)]
        else:
            raise ValueError(f"未知检索方式：{mode}")

        fused = [i for i, _ in rrf_fuse(rankings)]    # 两路按名次融合
        if dedupe_docs:
            fused = self._dedupe_by_doc(fused, limit=recall)
        cand = [self.chunks[i] for i in fused[:recall]]

        if use_rerank and cand:
            ranked = rerank(query, [c["text"] for c in cand],
                            top_n=min(k, len(cand)))
            pairs = [(fused[i], score) for i, score in ranked]
        else:
            pairs = [(i, 1.0 / (rank + 1)) for rank, i in enumerate(fused[:k])]

        out = []
        for rank, (i, score) in enumerate(pairs, 1):
            c = dict(self.chunks[i])
            c["score"] = round(float(score), 4)
            c["rank"] = rank
            out.append(c)
        return out
