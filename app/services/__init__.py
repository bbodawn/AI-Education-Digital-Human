"""服务层：对外提供服务实例。"""

from functools import lru_cache

from app.config import BASE_DIR, settings
from app.services.asr import ASRService, FasterWhisperService
from app.services.digital_human import DigitalHumanService, LiveTalkingService
from app.services.llm import LLMService, OllamaService


@lru_cache
def get_service() -> DigitalHumanService:
    """返回数字人服务实例（V0.2 单例）。

    路由通过 FastAPI 的 Depends 取用。测试用 app.dependency_overrides 覆盖它，
    注入一个带 httpx.MockTransport 的实例，从而不必真的启动 LiveTalking。
    """
    return LiveTalkingService(settings.avatar_base_url)


@lru_cache
def get_llm_service() -> LLMService:
    """返回大模型服务实例（V0.2 单例）。

    同样通过 Depends 取用，测试用 dependency_overrides 换成假的模型。
    """
    return OllamaService(settings.ollama_base_url, settings.ollama_model)


@lru_cache
def get_asr_service() -> ASRService:
    """返回进程内复用的 ASR 模型实例。"""
    return FasterWhisperService(
        model=settings.asr_model,
        device=settings.asr_device,
        compute_type=settings.asr_compute_type,
        language=settings.asr_language,
        download_root=BASE_DIR / "models",
    )
