"""工具契约：每个工具 = 名称 + 描述 + 参数模型(pydantic) + 异步处理器。

参数校验用 pydantic 模型而不是手写 JSON Schema：schema 自动生成喂给 LLM，
校验错误自动变成可读信息回喂给模型让它自纠——错误是数据，不是异常。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[BaseModel], Awaitable[str]]  # 返回给模型看的字符串


@dataclass
class ToolCallOutcome:
    content: str  # 回喂给模型的内容；出错时也是它（错误即反馈）
    is_error: bool = False


class ToolRegistry:
    """注册表 + 调度。所有失败路径都返回 outcome 而不抛异常——
    单个工具的任何问题都不允许中断整次任务。"""

    _ERROR_PREFIX = "[TOOL_ERROR]"

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重名: {tool.name}")
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def openai_specs(self) -> list[dict[str, Any]]:
        """生成传给 chat.completions 的 tools 参数。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.args_model.model_json_schema(),
                },
            }
            for t in self._tools.values()
        ]

    async def dispatch(self, name: str, arguments_raw: str) -> ToolCallOutcome:
        """执行一次工具调用。参数非法/工具异常 → 错误字符串，绝不 raise。"""
        tool = self._tools.get(name)
        if tool is None:
            return ToolCallOutcome(
                f"{self._ERROR_PREFIX} 未知工具 {name!r}，可用工具: {self.names()}", True
            )
        try:
            args = tool.args_model.model_validate_json(arguments_raw)
        except ValidationError as e:
            return ToolCallOutcome(f"{self._ERROR_PREFIX} 参数无效({name}): {e}", True)
        try:
            return ToolCallOutcome(await tool.handler(args), False)
        except Exception as e:  # noqa: BLE001 —— 工具异常同样是对模型的可读反馈
            return ToolCallOutcome(
                f"{self._ERROR_PREFIX} 工具执行失败({name}): {type(e).__name__}: {e}", True
            )
