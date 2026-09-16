"""服务层：对外提供服务实例。"""

from functools import lru_cache

from app.config import settings
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
