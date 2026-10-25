"""fetch_page 输出契约 —— W2 的"W0"：先定数据形状，再写抓取代码。

为什么契约先行（与 W0 同一原则）：Claim.quote_locator 的格式 W0 就埋下了
（`{落盘原文路径}#{起始}-{结束}`）。fetch_page 的输出若对不上，这个字段
永远填不上，"人工抽检一键回查原句"就是空话——等实现完才发现，
Claim / Verifier / 抽检三头返工。

一次 fetch_page 的数据分三层，各有生命周期：

    raw html   data/pages/{page_id}.html   只为保真与免重抓；永不进上下文/trace
    归档正文   data/pages/{page_id}.txt    偏移量的锚定文本；写定后不可变
    相关片段   1~3 段 + codepoint 偏移      唯一进模型上下文的部分

三条硬规则（针对"数字不可信"类审计洞的预防性设计）：
1. passage 必须是归档正文的逐字子串，且长度 == 偏移跨度——片段不是摘要，
   是切片，schema 层机检；
2. 模型永远不报数字偏移：researcher 引用 snippet_id + 原文引句，绝对偏移由
   代码定位。不信任自报、只信程序生成——第三例（前两例：citation_map、
   word_count）；
3. 偏移按 UTF-8 解码后的 codepoint 计（Python str 索引），不是字节偏移。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# 契约基类与 schemas 共用（extra=forbid：契约比业务代码更硬）
from src.agent.schemas import Contract


class FetchPageArgs(BaseModel):
    """fetch_page 入参。query 不是给模型看的装饰——是切段后 BM25 相关性
    排序的依据，通常传当前子问题或搜索词。"""

    url: str
    query: str = Field(min_length=1, description="片段相关性排序用（BM25 query）")
    max_snippets: int = Field(default=3, ge=1, le=3)  # PLAN：每页只留 1~3 片段


class PageSnippet(Contract):
    """一段与 query 相关的归档正文切片。进入模型上下文的全部内容就是 passage。"""

    page_id: str
    char_start: int = Field(ge=0, description="归档正文中的起点（codepoint，含）")
    char_end: int = Field(description="归档正文中的终点（codepoint，不含）")
    passage: str = Field(description="片段正文：归档正文的逐字子串，禁止改写/摘要")
    url: str
    published_at: date | None = Field(
        default=None,
        description="页面标注的发布/更新日；None=取不到，Claim.as_of_date 回退用抓取日",
    )

    @model_validator(mode="after")
    def _span_sane(self) -> PageSnippet:
        if self.char_end <= self.char_start:
            raise ValueError(f"偏移非法: [{self.char_start}, {self.char_end})")
        # ★ 切片 ≠ 摘要的机检：跨度长度必须等于 passage 长度，
        # 改写过/截断过的"片段"在构造时就被拒绝
        if self.char_end - self.char_start != len(self.passage):
            raise ValueError(
                f"passage 长度({len(self.passage)})与偏移跨度"
                f"({self.char_end - self.char_start})不一致：片段必须是逐字切片"
            )
        return self

    @property
    def snippet_id(self) -> str:
        """researcher 结构化输出引用片段的方式。随 to_model_text 输出给模型，
        由模型原样回传（同 tool_call id 的机制），模型不报数字偏移。"""
        return f"{self.page_id}:{self.char_start}-{self.char_end}"

    @property
    def locator(self) -> str:
        """Claim.quote_locator 的直接填法，格式与 W0 埋的字段严格一致。"""
        return f"data/pages/{self.page_id}.txt#{self.char_start}-{self.char_end}"

    def verify(self, archive_text: str) -> bool:
        """抽检/测试用：passage 是否真是归档正文的逐字子串。"""
        return archive_text[self.char_start : self.char_end] == self.passage

    def locate_quote(
        self, quote: str
    ) -> tuple[int, int] | Literal["ambiguous"] | None:
        """在片段内定位引文，返回**归档正文坐标系**的绝对跨度。

        三种结局，绝不猜：
        - (start, end)：唯一定位成功
        - "ambiguous"：引文在片段中出现多次——锚哪一处都是猜，交调用方
          失败处理（回喂模型"引用更长上下文"）
        - None：找不到（含空白容忍路径）

        匹配两级：先逐字 find；失败再空白容忍（模型常把换行/连续空白
        折叠成单空格，按非空白段以 \\s+ 连接重搜）。
        """
        q = quote.strip()
        if not q:
            return None
        if q in self.passage:
            if self.passage.count(q) > 1:
                return "ambiguous"
            idx = self.passage.find(q)
            return self.char_start + idx, self.char_start + idx + len(q)
        parts = [re.escape(p) for p in q.split()]
        if not parts:
            return None
        matches = list(re.finditer(r"\s+".join(parts), self.passage))
        if not matches:
            return None
        if len(matches) > 1:
            return "ambiguous"
        m = matches[0]
        return self.char_start + m.start(), self.char_start + m.end()


class FetchedPage(Contract):
    """一次 fetch_page 的完整产出（落库/统计/归档管理用）。
    给模型看的只有 to_model_text()——三层里唯独片段走这条路。"""

    page_id: str = Field(
        description="sha1(url)[:12]-YYYYMMDDHHMMSS-uuid4[:6]；随机后缀保证"
        "同秒重抓也必出新 id（时间戳秒级会撞号→归档被覆盖），归档不可变"
    )
    url: str
    fetched_at: datetime
    via: Literal["direct", "jina"] = Field(
        description="抓取通道：默认直抓+trafilatura，Jina Reader 仅作增强（免费限速紧）"
    )
    title: str | None = None
    html_path: str = Field(description="raw html 归档路径（相对项目根）")
    text_path: str = Field(description="归档正文路径 = 偏移锚定文本")
    text_chars: int = Field(ge=0, description="归档正文长度（codepoint）")
    n_passages: int = Field(ge=0, description="切段数；正文抽取失败为 0")
    snippets: list[PageSnippet] = Field(default_factory=list, description="BM25 选出的 1~3 段")
    elapsed_s: float = 0.0

    @model_validator(mode="after")
    def _snippets_belong_and_in_range(self) -> FetchedPage:
        for s in self.snippets:
            if s.page_id != self.page_id:
                raise ValueError(f"片段 {s.snippet_id} 不属于本页 {self.page_id}")
            if s.char_end > self.text_chars:
                # 片段越界 = 偏移造假（指不到归档正文里），当场抓住
                raise ValueError(
                    f"片段偏移越界: {s.snippet_id} end={s.char_end} > 正文 {self.text_chars} 字符"
                )
        return self

    def to_model_text(self) -> str:
        """进模型上下文的紧凑表示：标题 + URL + 带 id 的片段，仅此而已。
        片段携带 snippet_id——W3 researcher 在结构化输出里原样回传它。"""
        if not self.snippets:
            return f"【网页】{self.url}\n(正文抽取失败或无可读内容)"
        lines = [f"【网页】{self.title or '(无标题)'}", self.url, "---"]
        for i, s in enumerate(self.snippets, 1):
            lines.append(f"[片段{i}/{len(self.snippets)} · id={s.snippet_id}] {s.passage}")
        return "\n".join(lines)
