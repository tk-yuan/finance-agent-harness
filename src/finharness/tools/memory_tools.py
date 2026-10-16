"""长期记忆工具：让 Agent 自己判断什么值得记下来。

## 为什么需要「写入」这一步

`context/memory.py` 里那套机制（权重、衰减、强化、淘汰、召回）一直是完整的，
但**没有入口往里写**——只有单元测试在调 `remember()`，源码里一个调用都没有。
结果就是：机制跑得通，但记忆库永远是空的。

这个工具补的就是那个入口：**由模型自己判断"这条值得记"**，然后存下来。

## 写入的边界（和 ask_user 一样，得写死）

模型很容易把记忆当垃圾桶——什么都说"值得记"。
所以描述里给了明确的两边：

  · 该记：用户纠正过你的做法、企业特有的业务口径、你踩过的坑
  · 不该记：一次性的东西（这个月的金额、这一笔账）、账套里查得到的

**判断标准一句话**：下次开一个新对话，这条还用得上吗？
用得上就记，用不上就别占地方——记忆库不是日志。

## 写进来之后会发生什么

不需要模型操心。召回、衰减、强化、淘汰全由 `LayeredMemory` 管：

  · 下次对话开始，`before_model` 会按「关键词重合 × 权重」自动挑几条塞进提示词
  · 被用到的记忆权重变高（用得越多越重）
  · 没人用的每 14 天权重减半，低到一定程度被淘汰

**写入是模型的活，存取和淘汰是代码的活**——这个分工是刻意的。
"""

from __future__ import annotations

from ..runtime.tools import Tool, ToolRegistry

_KINDS = ("pattern", "policy_note", "failure", "preference")

_DESCRIPTION = (
    "把一条**跨会话还用得上**的经验记下来，以后新开对话会自动想起来。\n"
    "**判断标准就一句：下次开新对话，这条还用得上吗？**\n"
    "  · 该记：用户纠正过你的做法（「我们公司房租不走管理费用」）；"
    "企业特有的口径和习惯；你踩过的坑和后来怎么解决的\n"
    "  · 不该记：一次性的东西（这个月的金额、这一笔的科目）；"
    "账套里查得到的（余额、往来、科目表）；用户的临时交代\n"
    "**记忆库不是日志**，塞满噪音会让真正有用的经验找不到。一次只记一条，写清楚、别写长。"
)

_PARAMS = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": "要记的内容，一句话。写成以后你自己看得懂的样子——"
                           "比如「房租要记销售费用，不是管理费用（用户 2026-10 纠正过）」。",
        },
        "kind": {
            "type": "string",
            "enum": list(_KINDS),
            "description": "类型：pattern=业务口径/做法，policy_note=准则口径，"
                           "failure=踩过的坑，preference=用户偏好。默认 pattern。",
        },
        "tags": {
            "type": "string",
            "description": "可选：关键词，空格分隔。召回时靠它匹配，写两三个能提高命中率。",
        },
    },
    "required": ["content"],
}


def register_memory_tools(registry: ToolRegistry, memory) -> None:
    """把 remember 挂进工具池。

    没有 memory 时（理论上不会发生）工具照样注册但返回提示——保持工具池形状稳定，
    接线测试才有意义。
    """

    def remember(content: str, kind: str = "pattern", tags: str = "") -> dict:
        text = (content or "").strip()
        if not text:
            return {"error": "要记的内容是空的。"}
        if memory is None:
            return {"error": "当前环境没有挂载记忆库，这条没法存。"}
        k = kind if kind in _KINDS else "pattern"
        tag_list = [t for t in str(tags or "").replace(",", " ").split() if t]
        eid = memory.remember(k, text, tag_list)
        return {"ok": True, "id": eid, "kind": k, "tags": tag_list,
                "message": "已记住，以后相关的问题会自动想起来。"}

    registry.register(Tool(
        name="remember",
        description=_DESCRIPTION,
        parameters=_PARAMS,
        handler=remember,
        read_only=False,          # 它确实写文件（.cache/memory.jsonl）
        category="memory",
    ))
