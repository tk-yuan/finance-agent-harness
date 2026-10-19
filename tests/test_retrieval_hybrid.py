"""混合召回的内核：BM25、RRF 融合、文档级去重。"""

from pathlib import Path

from finharness.tools.bm25 import BM25, rrf_fuse
from finharness.tools.retrieval import PolicyIndex


def test_bm25_ranks_keyword_match_first():
    bm = BM25().fit([
        "企业应当计提存货跌价准备",
        "固定资产折旧年限为五年",
        "增值税小规模纳税人减免政策",
    ])
    hits = bm.search("存货跌价", top_k=3)
    assert hits and hits[0][0] == 0
    assert bm.search("完全不相关的词", top_k=3) == []


def test_rrf_prefers_documents_in_both_rankings():
    fused = rrf_fuse([[0, 1, 2], [1, 0, 3]])
    assert {i for i, _ in fused} == {0, 1, 2, 3}
    assert {fused[0][0], fused[1][0]} == {0, 1}   # 两路都出现的排前面
    assert fused[-1][0] == 3                      # 只出现一路的排最后


def test_dedupe_keeps_first_chunk_per_document():
    idx = PolicyIndex(Path("none.jsonl"), Path("none"))
    idx.chunks = [
        {"doc_id": "A", "text": "a1"},
        {"doc_id": "A", "text": "a2"},
        {"doc_id": "B", "text": "b1"},
        {"doc_id": "C", "text": "c1"},
    ]
    assert idx._dedupe_by_doc([0, 1, 2, 3], limit=10) == [0, 2, 3]
    assert idx._dedupe_by_doc([0, 1, 2, 3], limit=2) == [0, 2]
