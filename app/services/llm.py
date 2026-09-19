"""本地大模型的抽象，以及基于 Ollama 的实现。

分层的意义：业务代码只认识 LLMService，不认识 Ollama。将来换成别的本地推理
框架（vLLM、llama.cpp 等）只需要新增一个实现类。

这一层只负责「把问题变成一段话」，不负责播报——播报是 DigitalHumanService 的事。
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

import httpx

from app.conversation import ChatMessage

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 错误契约（与数字人服务同样的思路：上层不该看见 httpx 的异常类型）
# --------------------------------------------------------------------------


class LLMError(Exception):
    """大模型调用失败的基类。"""


class LLMUnavailable(LLMError):
    """连不上 Ollama：服务没启动、地址不对、网络不通或超时。"""


class LLMBadResponse(LLMError):
    """Ollama 的响应不可用：非 2xx，或 2xx 但答不出内容。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMService(ABC):
    """大模型服务接口。业务层只依赖它。"""

    @abstractmethod
    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        instruction: str | None = None,
    ) -> str:
        """把标准对话消息交给模型，返回一段可直接朗读的纯文本回答。"""


class OllamaService(LLMService):
    """通过 Ollama 的 HTTP API 调用本地模型。"""

    # 生成本来就慢（7B 模型首答要几秒），这个超时是给「模型思考」用的，
    # 和数字人那边 10 秒的「请求受理」超时不是一个量级。
    _TIMEOUT = 120.0

    # 系统提示词的重点不是「答得好」，而是「答得适合念出来」：
    # 数字人是靠 TTS 逐字读的，Markdown 记号会被念成奇怪的东西，
    # 太长则会让用户干等。
    SYSTEM_PROMPT = (
        "你是一名友好的AI数字人教育教练。\n\n"
        "请用清晰、简洁、适合口头表达的中文回答用户问题。\n"
        "避免使用复杂的 Markdown 格式。\n"
        "回答不要过长，优先控制在 100～200 个中文字符以内。\n"
        "如果问题比较复杂，可以分点说明，但要适合数字人口播。"
    )

    def __init__(
        self,
        base_url: str,
        model: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        # transport 仅供测试注入 httpx.MockTransport，生产路径保持为 None。
        self._transport = transport

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        instruction: str | None = None,
    ) -> str:
        system_content = self.SYSTEM_PROMPT
        if instruction and instruction.strip():
            system_content = f"{system_content}\n\n{instruction.strip()}"

        payload = await self._request(
            "/api/chat",
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system_content},
                    *messages,
                ],
                "stream": False,
            },
        )

        # Ollama 的非流式应答形如 {"message": {"role": "assistant", "content": "..."}}
        answer = (payload.get("message") or {}).get("content", "").strip()
        if not answer:
            # 空回答不能一路传到 TTS——那时数字人只会「不说话」，
            # 而问题要到很远的地方才看得出来。
            raise LLMBadResponse("模型返回了空回答")
        return answer

    def _build_client(self) -> httpx.AsyncClient:
        """构造 HTTP 客户端。

        与数字人服务同样的理由：trust_env=False 绕开系统代理
        （Windows 注册表里的代理设置会把发往 127.0.0.1 的请求也送进代理）。
        """
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._TIMEOUT,
            transport=self._transport,
            trust_env=False,
        )

    async def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """向 Ollama 发一次请求，并把失败归纳成上层的错误类型。

        刻意不记录 payload —— 用户问题和回答没必要整段进日志。
        """
        async with self._build_client() as client:
            try:
                response = await client.post(path, json=payload)
            except httpx.RequestError as exc:
                logger.warning(
                    "Ollama 不可达：POST %s（%s）", path, exc.__class__.__name__
                )
                raise LLMUnavailable(f"无法连接 Ollama: POST {path}") from exc

        if response.status_code >= 400:
            logger.warning("Ollama 返回错误：POST %s -> HTTP %s", path, response.status_code)
            raise LLMBadResponse(
                f"POST {path} -> HTTP {response.status_code}", response.status_code
            )

        try:
            return response.json()
        except ValueError as exc:
            logger.warning("Ollama 应答不是合法 JSON：POST %s", path)
            raise LLMBadResponse(f"POST {path} 的应答不是合法 JSON") from exc
