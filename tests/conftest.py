"""共享测试工具。"""

from src.agent.llm import LLMResult


class SmartFakeLLM:
    """按脚本回放的假 LLM。

    脚本项可以是 LLMResult / 异常 / **可调用对象**（入参 messages，返回
    LLMResult）——可调用项用于模拟"读取工具结果后动态决定下一轮"的真实
    行为（比如从 fetch_page 返回里抄 snippet_id，和真模型做的事一样）。
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[list[dict]] = []

    async def complete(self, messages, tools=None, **kwargs):
        self.calls.append([dict(m) for m in messages])
        item = self.script.pop(0)
        if callable(item):
            return item(messages)
        if isinstance(item, Exception):
            raise item
        return item


def final(text: str) -> LLMResult:
    return LLMResult(content=text)
