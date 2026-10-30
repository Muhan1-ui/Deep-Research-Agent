"""集中配置：环境变量 + .env（复制 .env.example 为 .env 后填写）。

字段与 .env.example 一一对应；测试里用 kwargs 显式传值，不依赖环境。
"""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---- LLM：开发期可切 GLM 免费额度，正式跑批用 DeepSeek ----
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"  # 型号以 GET /models 为准（2026-10 校验：deepseek-flash / deepseek-v4-pro）
    llm_timeout_s: float = 120.0  # read 超时；SDK 默认 600s 会让批跑挂在僵死连接上
    strong_llm_model: str = ""  # planner/writer 用；空则回落 llm_model
    # 思考模式：deepseek-v4-flash 默认开启。默认 disabled 的三个理由：
    # ① 思考 + tools 的多轮请求必须回传 reasoning_content，缺失 400（loop 已支持回传，
    #    但先关省心）；② 思考 token 按输出计费，researcher 高频调用是纯开销；
    # ③ 思考模式下 temperature 静默失效——评测 judge 想要 temp=0 就必须关思考。
    llm_thinking: Literal["enabled", "disabled", "auto", "low", "high", "max"] = "disabled"  # auto=不干预，用服务端默认

    # ---- 评测 judge：必须与被评系统不同厂商（防同源偏差，见 schemas.py）----
    judge_api_key: str = ""
    judge_base_url: str = ""
    judge_model: str = ""
    # judge 的思考档位（GLM-5.3 强制思考，只能用 low/high/max 表达）。
    # 校准实测：low 档忽略 rubric 的边界规则（同批 30 条 27/30），high 档 4/4 边界用例判对（28/30）。
    # 默认 high；写进 EvalRecord.judge_effort 与报告首行，作为复现参数。
    judge_thinking: Literal["auto", "low", "high", "max"] = "high"

    # ---- 搜索 ----
    tavily_api_key: str = ""

    # ---- 本地路径 ----
    database_path: str = "data/agent.db"
    trace_dir: str = "data/traces"
    pages_dir: str = "data/pages"
