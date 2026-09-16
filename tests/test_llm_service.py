"""验证 OllamaService 与 Ollama HTTP 契约的对齐，以及失败如何暴露。

这一层是「问题的答案从哪里来」的唯一入口。它出错的方式很隐蔽——
模型答不出东西时不会抛异常，只会返回空字符串，然后数字人一声不吭，
问题要到很远的地方才看得出来。所以这里重点测「失败有没有被暴露」，
而不只是「成功路径能不能走通」。
"""

import asyncio
import json

import httpx
import pytest

from app.services.llm import (
    LLMBadResponse,
    LLMUnavailable,
    OllamaService,
)

BASE_URL = "http://127.0.0.1:11434"
MODEL = "qwen2.5:7b"
QUESTION = "什么是 RAG？"
ANSWER = "RAG 是检索增强生成，先检索资料再让模型作答。"


def build_service(recorder, responses=None, raise_error=None, model=MODEL):
    """构造一个会记录请求、但不做真实网络调用的 OllamaService。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if raise_error is not None:
            raise raise_error
        recorder.append(request)
        body = (responses or {}).get(
            request.url.path,
            {"message": {"role": "assistant", "content": ANSWER}},
        )
        return httpx.Response(200, json=body)

    return OllamaService(base_url=BASE_URL, model=model, transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------
# 成功路径
# --------------------------------------------------------------------------


def test_chat_returns_model_answer() -> None:
    requests = []
    service = build_service(requests)

    answer = asyncio.run(service.chat(QUESTION))

    assert answer == ANSWER
    assert requests[0].url.path == "/api/chat"
    assert requests[0].method == "POST"


def test_chat_sends_system_prompt_and_question() -> None:
    """系统提示词必须真的发出去。

    数字人是用 TTS 逐字念的，没有这段约束，模型会回 Markdown 和长篇大论，
    念出来既难听又让用户干等。
    """
    requests = []
    service = build_service(requests)

    asyncio.run(service.chat(QUESTION))

    payload = json.loads(requests[0].content)
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["system", "user"]
    assert payload["messages"][0]["content"] == OllamaService.SYSTEM_PROMPT
    assert payload["messages"][1]["content"] == QUESTION
    assert payload["stream"] is False
    assert payload["model"] == MODEL


def test_chat_uses_configured_model() -> None:
    requests = []
    service = build_service(requests, model="qwen2.5:0.5b")

    asyncio.run(service.chat(QUESTION))

    assert json.loads(requests[0].content)["model"] == "qwen2.5:0.5b"


def test_chat_strips_whitespace_around_answer() -> None:
    requests = []
    service = build_service(
        requests,
        responses={"/api/chat": {"message": {"role": "assistant", "content": "  你好  \n"}}},
    )

    assert asyncio.run(service.chat(QUESTION)) == "你好"


# --------------------------------------------------------------------------
# 失败路径 —— 必须显式抛错，不能把空回答当成正常结果传下去
# --------------------------------------------------------------------------


def test_empty_answer_raises_bad_response() -> None:
    """空回答必须当场报错。

    否则空字符串会一路传到 TTS，数字人只是「不说话」，
    排查时根本看不出是哪一层的问题。
    """
    service = build_service(
        [], responses={"/api/chat": {"message": {"role": "assistant", "content": "   "}}}
    )

    with pytest.raises(LLMBadResponse):
        asyncio.run(service.chat(QUESTION))


def test_missing_message_field_raises_bad_response() -> None:
    service = build_service([], responses={"/api/chat": {"done": True}})

    with pytest.raises(LLMBadResponse):
        asyncio.run(service.chat(QUESTION))


def test_http_error_raises_bad_response() -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="model not found")

    service = OllamaService(
        base_url=BASE_URL, model=MODEL, transport=httpx.MockTransport(failing)
    )

    with pytest.raises(LLMBadResponse) as excinfo:
        asyncio.run(service.chat(QUESTION))

    assert excinfo.value.status_code == 500


def test_connection_error_raises_unavailable() -> None:
    """Ollama 没启动时应当是 LLMUnavailable（映射为 502）。"""
    service = build_service([], raise_error=httpx.ConnectError("connection refused"))

    with pytest.raises(LLMUnavailable):
        asyncio.run(service.chat(QUESTION))


def test_timeout_raises_unavailable() -> None:
    """模型生成超时也要归到「服务不可用」，而不是抛裸的 httpx 异常。"""
    service = build_service([], raise_error=httpx.ReadTimeout("timed out"))

    with pytest.raises(LLMUnavailable):
        asyncio.run(service.chat(QUESTION))


def test_non_json_response_raises_bad_response() -> None:
    def not_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    service = OllamaService(
        base_url=BASE_URL, model=MODEL, transport=httpx.MockTransport(not_json)
    )

    with pytest.raises(LLMBadResponse):
        asyncio.run(service.chat(QUESTION))


def test_requests_go_to_configured_base_url() -> None:
    """地址完全由配置决定，类内不硬编码 —— 换本地/远端推理服务只改配置。"""
    requests = []
    service = build_service(requests, model=MODEL)
    service._base_url = "http://10.0.0.9:11434"

    asyncio.run(service.chat(QUESTION))

    assert requests[0].url.host == "10.0.0.9"
    assert requests[0].url.port == 11434


def test_http_client_ignores_system_proxy() -> None:
    """与数字人服务同样的理由：绕开 Windows 系统代理，直连本地 11434。"""
    client = OllamaService(base_url=BASE_URL, model=MODEL)._build_client()
    asyncio.run(client.aclose())

    assert client.trust_env is False
