"""应用配置。

配置一律来自环境变量（可写在 .env 中），代码里不硬编码服务地址。
这样切换「本地 LiveTalking」与「云端 LiveTalking」只需改一个环境变量。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置项。环境变量名与字段名大小写不敏感，例如 AVATAR_BASE_URL。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 数字人服务（LiveTalking）的基地址
    avatar_base_url: str = "http://127.0.0.1:8010"


settings = Settings()
