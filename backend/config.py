"""配置管理：从.env加载全部配置项，缺失必需字段时启动报错。"""
import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置。必需字段无默认值，缺失即抛出ValidationError，禁止静默回退。"""

    # 必需字段
    MODEL_API_BASE: str = Field(..., description="主LLM模型API地址")
    MODEL_NAME: str = Field(..., description="主LLM模型名称")
    CONTEXT_TURNS: int = Field(..., description="保留的对话轮数")
    # 等待汇总配置
    WAIT_SLIDE_SECONDS: int = Field(..., description="滑动窗口等待秒数")
    WAIT_MAX_SECONDS: int = Field(..., description="最长等待秒数")
    # 会话提炼：机器人回复后静默无新消息达到该秒数即判定会话结束，触发Qwen提炼
    SESSION_END_SECONDS: int = Field(default=20, description="会话结束静默判定秒数")
    KNOWLEDGE_BASE_PATH: str = Field(..., description="知识库文件路径")
    DECISION_ENGINE: str = Field(..., description="决策引擎: rule/jev/kev/qwen")

    # 模型可选配置
    MODEL_API_KEY: str = ""
    TEMPERATURE: float = 0.3
    # 兼容保留：已按路径拆分为 DECISION_MAX_TOKENS / GEN_MAX_TOKENS，本值不再单独生效
    MAX_TOKENS: int = 512

    # 思考模式与 max_tokens 按路径拆分（Qwen3.8-27B 实测 2026-10-22）：
    # 思考开关必须走 chat_template_kwargs（顶层 enable_thinking 无效）；
    # 思考开 + max_tokens<512 会把 token 全花在思考上导致正文为空。
    DECISION_MAX_TOKENS: int = 256      # 意图快判（关思考，JSON 约 56 tokens）
    GEN_MAX_TOKENS: int = 1024          # 文字答复生成（思考开）
    DECISION_ENABLE_THINKING: bool = False
    GEN_ENABLE_THINKING: bool = True

    # Jev 渠道链（2026-10-22 用户定案）：逗号分隔渠道名，按序降级，全挂才回退。
    # 每渠道 4 项 JEV_<NAME大写>_BASE_URL/_ENDPOINT/_MODEL/_API_KEY，
    # 任一为空跳过该渠道。endpoint 路径不同：官方/OpenCode 走 /v1/systemone，
    # OpenRouter 走 /alpha/decisions，别混用。
    JEV_CHANNELS: str = "typesafe,opencode_free,opencode_pay,openrouter"
    JEV_TYPESAFE_BASE_URL: str = "https://api.typesafe.ai"
    JEV_TYPESAFE_ENDPOINT: str = "/v1/systemone"
    JEV_TYPESAFE_MODEL: str = "jev-latest"   # 官方 /v1/models 实查只有 jev-latest/jev-preview，jev-1.13 会 400
    JEV_TYPESAFE_API_KEY: str = ""
    JEV_OPENCODE_FREE_BASE_URL: str = "https://opencode.ai/inference"
    JEV_OPENCODE_FREE_ENDPOINT: str = "/v1/systemone"
    JEV_OPENCODE_FREE_MODEL: str = "jev-1.13-free"
    JEV_OPENCODE_FREE_API_KEY: str = ""
    JEV_OPENCODE_PAY_BASE_URL: str = "https://opencode.ai/inference"
    JEV_OPENCODE_PAY_ENDPOINT: str = "/v1/systemone"
    JEV_OPENCODE_PAY_MODEL: str = "jev-1.13"
    JEV_OPENCODE_PAY_API_KEY: str = ""
    JEV_OPENROUTER_BASE_URL: str = "https://openrouter.ai/api"
    JEV_OPENROUTER_ENDPOINT: str = "/alpha/decisions"
    JEV_OPENROUTER_MODEL: str = "typesafe/jev-1.13"
    JEV_OPENROUTER_API_KEY: str = ""

    # 话术配置
    FIRST_MESSAGE_GREETING: str = "您好，我是SDWAN智能客服机器人，很高兴为您服务。"
    NO_ANSWER_MESSAGE: str = "暂时无法回答，需要人工介入"

    # 决策引擎可选配置
    JEV_API_KEY: str = ""
    JEV_API_BASE: str = "https://openrouter.ai/api"
    JEV_MODEL: str = "typesafe/jev-1.13"
    KEV_MODEL_PATH: str = "tt-hous/kev-0.5b"
    KEV_DEVICE: str = "cpu"
    # Kev本地serve（kev包的python -m kev.serve）：非空时KevEngine走HTTP模式
    KEV_SERVE_URL: str = ""
    KEV_API_KEY: str = ""

    # 日志与服务
    LOG_LEVEL: str = "INFO"
    SAVE_CONVERSATIONS: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    model_config = SettingsConfigDict(
        # 允许用环境变量ENV_FILE指定配置文件（测试用），默认项目根目录.env
        env_file=os.getenv("ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
