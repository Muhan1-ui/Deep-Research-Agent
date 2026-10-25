"""集中配置：环境变量 + .env（复制 .env.example 为 .env 后填写）。

字段与 .env.example 一一对应；测试里用 kwargs 显式传值，不依赖环境。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---- LLM：开发期可切 GLM 免费额度，正式跑批用 DeepSeek ----
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    strong_llm_model: str = ""  # planner/writer 用；空则回落 llm_model

    # ---- 评测 judge：必须与被评系统不同厂商（防同源偏差，见 schemas.py）----
    judge_api_key: str = ""
    judge_base_url: str = ""
    judge_model: str = ""

    # ---- 搜索 ----
    tavily_api_key: str = ""

    # ---- 本地路径 ----
    database_path: str = "data/agent.db"
    trace_dir: str = "data/traces"
    pages_dir: str = "data/pages"
