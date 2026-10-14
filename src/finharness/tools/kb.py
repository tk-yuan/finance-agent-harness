"""知识库工具：检索财会准则与会计制度。

## 它和姊妹项目 `finance-rag-eval` 的关系

语料是同一份：财政部会计司官网抓的 **549 份准则/制度/解读原文**。

    那个项目  评测这份语料的检索质量——切分策略、召回、重排、消融、指标
    本项目    把检索当 Agent 的一个工具用

**为什么不直接引用那个项目的代码**：作品集项目要能各自克隆下来独立跑。
所以这里内置一个精简检索内核（`retrieval.py`），语义与那边一致但不做评测。
规模真的需要扩到十万级片段时，把 `retrieval.py` 换成向量库即可，
上层工具接口不动。

## 为什么 Agent 需要它

会计科目和记账规则**不该写死在提示词里**。企业会计政策会变、准则会更新，
写死就意味着改规则要改代码 + 重新发版。

放进知识库后：Agent 分类之前**先检索企业自己的规则**，
规则更新只需重新灌库——这是"企业私有财务知识库"的核心价值。
"""

from __future__ import annotations

from functools import lru_cache

from ..config import CACHE_DIR, DATA_DIR
from ..runtime.tools import Tool, ToolRegistry
from .retrieval import PolicyIndex


@lru_cache(maxsize=1)
def kb_index() -> PolicyIndex:
    return PolicyIndex(DATA_DIR / "corpus.jsonl", CACHE_DIR / "kb_index")


def search_accounting_rules(query: str, k: int = 3) -> dict:
    """检索财会准则原文。

    返回带**文号和施行日期**的片段——财务结论必须可溯源：
    说"按准则应该计入当期损益"没有意义，得说清是**哪一条**这么规定的。
    """
    if not query.strip():
        return {"error": "检索词不能为空"}
    try:
        hits = kb_index().search(query, k=k)
    except FileNotFoundError:
        return {"error": "知识库索引尚未建立，请先运行 scripts/build_kb_index.py"}

    return {
        "query": query,
        "count": len(hits),
        "results": [
            {
                "rank": h["rank"],
                "title": h.get("title", ""),
                "doc_no": h.get("doc_no", ""),
                "issued": h.get("issued", ""),
                "effective_from": h.get("effective_from", ""),
                "score": h.get("score", 0.0),
                "text": h["text"][:600],
            }
            for h in hits
        ],
    }


def register_kb_tools(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="search_accounting_rules",
        description="检索财会准则与会计制度原文。不确定一笔业务该记哪个科目、"
                    "或者想确认某类支出的会计处理依据时，先查这里——"
                    "返回带文号和施行日期的原文片段，结论可溯源。",
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "检索词，如「固定资产折旧年限」"},
            "k": {"type": "integer", "description": "返回条数", "default": 3},
        }, "required": ["query"]},
        handler=search_accounting_rules, read_only=True, category="knowledge",
    ))
