"""Planner：研究问题 → 3~6 个互相独立、可并行的子问题。

"模型报内容、代码定结构"的又一例：模型只输出 question + search_hints，
sub_query_id（SQ1..SQn）由代码分配——id 是 join key，格式不能交给模型。
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

MIN_SUB_QUERIES = 3
MAX_SUB_QUERIES = 6

PLANNER_PROMPT = """你是研究规划师。把用户的研究问题分解为 3~6 个互相独立、可并行检索的子问题。
要求：
1. 子问题合起来完整覆盖原问题，彼此不重复、无依赖；
2. 每个子问题附带 1~3 个建议搜索词；
3. 只输出 JSON 数组，每项形如 {"question": "...", "search_hints": ["..."]}，不要输出任何其他内容。"""


class PlannedItem(BaseModel):
    """模型输出的单个子问题（无 id——id 由代码分配）。"""

    model_config = ConfigDict(extra="forbid")

    question: str
    search_hints: list[str] = Field(default_factory=list)

    @field_validator("question")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("question 不能为空")
        return v.strip()


class SubQuery(BaseModel):
    """进入管线的子问题：代码分配的 id 是全系统 join key。"""

    sub_query_id: str
    question: str
    search_hints: list[str] = Field(default_factory=list)


class PlanError(Exception):
    """重试耗尽仍拿不到合规计划。"""


def extract_json_array(text: str) -> list:
    """从模型输出提取 JSON 数组：容忍 ```json 围栏与前后废话。"""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    start, end = t.find("["), t.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("输出中未找到 JSON 数组")
    data = json.loads(t[start : end + 1])
    if not isinstance(data, list):
        raise ValueError("顶层不是 JSON 数组")
    return data


def parse_plan(text: str) -> list[PlannedItem]:
    data = extract_json_array(text)
    items, errors = [], []
    for i, item in enumerate(data):
        try:
            items.append(PlannedItem.model_validate(item))
        except Exception as e:  # noqa: BLE001 —— 逐项收集，报给模型修
            errors.append(f"第 {i} 项不合规: {e}")
    if errors:
        raise ValueError("; ".join(errors))
    if not (MIN_SUB_QUERIES <= len(items) <= MAX_SUB_QUERIES):
        raise ValueError(f"子问题数量应为 {MIN_SUB_QUERIES}~{MAX_SUB_QUERIES}，实际 {len(items)}")
    return items


async def make_plan(llm, question: str, max_retries: int = 2) -> list[SubQuery]:
    """规划 + 解析失败回喂重试。返回带代码分配 id 的子问题列表。"""
    messages: list[dict] = [
        {"role": "system", "content": PLANNER_PROMPT},
        {"role": "user", "content": question},
    ]
    items: list[PlannedItem] | None = None
    last_err = ""
    for _ in range(max_retries + 1):
        result = await llm.complete(messages)
        messages.append({"role": "assistant", "content": result.content or ""})
        try:
            items = parse_plan(result.content or "")
            break
        except ValueError as e:
            last_err = str(e)
            messages.append({
                "role": "user",
                "content": f"你的输出不符合要求：{last_err}\n请修正后重新只输出合规 JSON 数组。",
            })
    if items is None:
        raise PlanError(f"规划失败（重试 {max_retries} 次）：{last_err}")
    return [
        SubQuery(sub_query_id=f"SQ{i + 1}", question=it.question, search_hints=it.search_hints)
        for i, it in enumerate(items)
    ]
