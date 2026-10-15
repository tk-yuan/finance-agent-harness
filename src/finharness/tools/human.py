"""人工介入工具：让 Agent 能主动向人提问。

## 为什么需要它

没有这个工具时，Agent 只有两条路：要么自己猜，要么整段放弃。
账目里「紧急采购-天津港进口贸易公司」这种单据，记办公费还是记主营业务成本**确实有解释空间**——
这时候正确的做法不是让模型掷硬币，而是**停下来问一句**。

## 和审批门的区别

    审批门（`post_voucher`）  人**否决权**——工具想动有外部后果的东西，要人点头
    提问（`ask_user`）        人**供给信息**——Agent 自己判断不了，缺的是一条信息

两者共用同一个阻塞机制（`ApprovalGate`），只是载荷不同。所以这里的实现很短：
借 `gate.ask()` 挂起，拿到回答就返回。

## 什么时候不该用它

模型很容易把它当成"偷懒出口"——遇到什么都问一句。
所以工具描述里写死了边界：**能从账套或准则查到的，一律自己查**。
真正的用法是"业务性质"这类账套里根本没有的信息。
"""

from __future__ import annotations

from ..runtime.tools import Tool, ToolRegistry

_DESCRIPTION = (
    "向用户提一个问题，以补齐从账套和准则里查不到的关键信息，然后等他回答。\n"
    "**只在确实判断不了时使用**。判断标准：\n"
    "  · 该用：业务性质类——这笔采购算办公费还是成本、这张票是不是重复报销、这笔款对应哪个合同\n"
    "  · 不该用：能查的一律自己查（余额、往来、账目、准则都查得到）；"
    "用户已经在话里说过的不要重复问；一次只问一个最关键的\n"
    "提完会**阻塞等待**，用户回答后你拿到答案继续。如果没人回答，"
    "你要基于现有信息做出合理假设，并在结论里显式标注哪部分是假设。"
)

_PARAMS = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "要问的问题。说清楚你为什么拿不准——只问「这个记哪个科目」而不给上下文，用户没法回答。",
        },
        "options": {
            "type": "string",
            "description": "可选：备选项，用 / 分隔，例如「办公费/主营业务成本/固定资产」。给了会更方便回答。",
        },
    },
    "required": ["question"],
}


def register_human_tools(registry: ToolRegistry, gate=None) -> None:
    """把 ask_user 挂进工具池。

    gate 为 None 时（CLI 单跑、单元测试）工具仍然注册，但调用会返回一条
    「当前没有可交互的用户」的提示——**注册不依赖 gate**，否则线上和测试环境的
    工具池形状就不一样了，接线测试也就失去意义。
    """

    def ask_user(question: str, options: str = "") -> dict:
        if gate is None:
            return {
                "error": "当前环境没有可交互的用户，请基于现有信息做出合理假设，"
                         "并在结论里显式标注这是假设。"
            }
        opts = [o.strip() for o in options.split("/") if o.strip()] if options else []
        req = gate.ask(question, opts)
        if req.status == "answered" and req.answer.strip():
            return {"ok": True, "answer": req.answer.strip(),
                    "waited_sec": req.waited_sec}
        return {
            "error": "用户没有在限定时间内回答。请基于现有信息做出合理假设继续推进，"
                     "并在结论里显式标注哪部分是假设、需要用户复核。"
        }

    registry.register(Tool(
        name="ask_user",
        description=_DESCRIPTION,
        parameters=_PARAMS,
        handler=ask_user,
        read_only=True,
        category="human",
    ))
