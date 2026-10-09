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
    MAX_TOKENS: int = 512

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
