"""应用配置。

配置一律来自环境变量（可写在 .env 中），代码里不硬编码服务地址。
这样切换「本地 LiveTalking」与「云端 LiveTalking」只需改一个环境变量。
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（app/ 的上一级）
BASE_DIR = Path(__file__).resolve().parent.parent

# 前端页面所在目录。用绝对路径，这样无论从哪个工作目录启动 uvicorn 都能找到。
STATIC_DIR = BASE_DIR / "static"


class Settings(BaseSettings):
    """全局配置项。环境变量名与字段名大小写不敏感，例如 AVATAR_BASE_URL。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 数字人服务（LiveTalking）的基地址
    avatar_base_url: str = "http://127.0.0.1:8010"

    # 本地大模型（Ollama）地址与模型名。回答由本地生成，
    # 不经过云端——云端的 LiveTalking 只负责把文本念出来。
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b"

    # 本地语音识别。V0.3 Phase 1 固定走 CPU INT8，避免与 Ollama 争抢显存。
    asr_model: str = "small"
    asr_device: str = "cpu"
    asr_compute_type: str = "int8"
    asr_language: str = "zh"


settings = Settings()
