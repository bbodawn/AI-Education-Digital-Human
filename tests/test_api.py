"""验证本地 FastAPI 的对外接口。

用 httpx.MockTransport 伪造 LiveTalking，因此不需要真的启动它。
断言的重点是四条边界：

  1. 浏览器只跟本地 FastAPI 说话（相对路径、不出现 LiveTalking 地址）
  2. FastAPI 转译给 LiveTalking 的内容正确（尤其是 type=echo）
  3. 会话不是本地创建的时，能反查出来并且选哪个是可见的
  4. LiveTalking 出问题时，浏览器收到可读的 JSON 而不是 Python traceback
"""

import asyncio
import json
from collections.abc import Sequence

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import chat
from app.conversation import ChatMessage, ConversationStore
from app.main import app
from app.schemas import ChatAskRequest
from app.services import get_llm_service, get_service
from app.services.digital_human import LiveTalkingService
from app.services.llm import LLMBadResponse, LLMService, LLMUnavailable
from app.session import clear_session_id, set_session_id

BASE_URL = "http://127.0.0.1:8010"
SESSION_ID = "4b0e14eb-b468-4058-a181-b44b3bba3e30"
NEWER_SESSION_ID = "a11113ff-1bbf-4080-aef4-578b63d78a30"
QUESTION = "什么是 RAG？"
MODEL_ANSWER = "RAG 是检索增强生成，先检索资料再让模型作答。"


class StubLLM(LLMService):
    """假的本地模型。记录被问了什么，并按需成功或失败。

    on_chat 用来模拟「生成期间发生的事」——比如用户点了打断。
    """

    def __init__(self, answer: str = MODEL_ANSWER, error: Exception | None = None, on_chat=None):
        self.answer = answer
        self.error = error
        self.on_chat = on_chat
        self.calls: list[list[ChatMessage]] = []

    async def chat(self, messages: Sequence[ChatMessage]) -> str:
        self.calls.append(
            [
                {"role": message["role"], "content": message["content"]}
                for message in messages
            ]
        )
        if self.on_chat is not None:
            self.on_chat()
        if self.error is not None:
            raise self.error
        return self.answer


class SequencedLLM(LLMService):
    """按调用顺序返回答案或抛异常，并记录每次收到的完整 messages。"""

    def __init__(self, outcomes, on_call=None):
        self.outcomes = list(outcomes)
        self.on_call = on_call
        self.calls: list[list[ChatMessage]] = []

    async def chat(self, messages: Sequence[ChatMessage]) -> str:
        self.calls.append(
            [
                {"role": message["role"], "content": message["content"]}
                for message in messages
            ]
        )
        call_number = len(self.calls)
        if self.on_call is not None:
            self.on_call(call_number)
        outcome = self.outcomes[call_number - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

OFFER_ANSWER = {"sdp": "mock-answer-sdp", "type": "answer", "sessionid": SESSION_ID}

# 后端各接口的默认应答。默认值刻意取「一切正常」，让单个用例只需要覆盖
# 它真正关心的那一个接口。
DEFAULT_RESPONSES = {
    "/offer": OFFER_ANSWER,
    "/api/admin/sessions": {
        "code": 0,
        "msg": "ok",
        "data": {"sessions": [{"sessionid": SESSION_ID}]},
    },
    "/is_speaking": {"code": 0, "msg": "ok", "data": True},
    "/human": {"code": 0, "msg": "ok", "data": {}},
    "/interrupt_talk": {"code": 0, "msg": "ok", "data": {}},
}


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch):
    """每个用例都从「没有会话」开始，并去掉播报轮询等待。"""
    clear_session_id()
    monkeypatch.setattr(chat, "conversation_store", ConversationStore())
    monkeypatch.setattr(chat, "_SPEAKING_POLL_ATTEMPTS", 2)
    monkeypatch.setattr(chat, "_SPEAKING_POLL_INTERVAL", 0)
    # 打断计数是模块级全局，不清零会让上一个用例的打断影响到下一个
    monkeypatch.setattr(chat, "_interrupt_epoch", 0)
    yield
    clear_session_id()
    app.dependency_overrides.clear()


def build_client(handler, llm: LLMService | None = None) -> TestClient:
    """把应用里的数字人服务（和模型）替换成测试替身。"""
    service = LiveTalkingService(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    app.dependency_overrides[get_service] = lambda: service
    app.dependency_overrides[get_llm_service] = lambda: llm or StubLLM()
    return TestClient(app)


def recording_handler(recorder, responses=None):
    """记录每次请求，并按 path 返回预设应答的假 LiveTalking。"""
    merged = {**DEFAULT_RESPONSES, **(responses or {})}

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        response = merged.get(request.url.path, {"code": 0, "msg": "ok", "data": {}})
        if callable(response):
            response = response()
        return httpx.Response(200, json=response)

    return handler


def paths_of(requests) -> list[str]:
    return [r.url.path for r in requests]


def connect_via_offer(client: TestClient) -> None:
    """走一遍 /offer 流程，让本地缓存里有一个 session_id。"""
    response = client.post(
        "/api/v1/avatar/webrtc/offer",
        json={"sdp": "browser-offer-sdp", "type": "offer"},
    )
    assert response.status_code == 200


# --------------------------------------------------------------------------
# 页面与健康检查
# --------------------------------------------------------------------------


def test_health_returns_ok() -> None:
    client = build_client(recording_handler([]))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_serves_frontend_page() -> None:
    """浏览器访问本地 FastAPI 根路径就能拿到前端，不需要额外起静态服务器。"""
    client = build_client(recording_handler([]))

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "AI 数字人教育教练" in response.text
    assert 'id="responseMetrics"' in response.text
    assert 'id="asrLatency">—' in response.text
    assert 'id="llmLatency">—' in response.text
    assert 'id="avatarLatency">—' in response.text
    assert 'id="totalLatency">—' in response.text
    assert "数字人起播确认" in response.text
    assert "if (!fromVoice) resetMetrics();" in response.text


def test_homepage_exposes_new_conversation_reset_controls() -> None:
    client = build_client(recording_handler([]))

    response = client.get("/")

    assert response.status_code == 200
    assert 'id="resetButton"' in response.text
    assert 'reset:     { path: "/api/v1/chat/reset" }' in response.text
    assert "await postJson(API.reset.path);" in response.text
    assert "resetButton.disabled = nextState !== RECORDING_STATE.IDLE || interactionBusy;" in response.text
    assert 'setHint("已开始新对话");' in response.text


def test_start_recording_preserves_existing_conversation_ui() -> None:
    client = build_client(recording_handler([]))

    response = client.get("/")
    start_recording = response.text.split(
        "async function startRecording()", 1
    )[1].split("function stopRecording()", 1)[0]

    assert "resetMetrics()" not in start_recording
    assert "answerBox.hidden" not in start_recording
    assert "answerText.textContent" not in start_recording
    assert "inputEl.value" not in start_recording
    assert "API.reset.path" not in start_recording


# --------------------------------------------------------------------------
# WebRTC 信令（保留，但已不是主路径——浏览器现在经 MediaMTX 拉流）
# --------------------------------------------------------------------------


def test_webrtc_offer_proxies_browser_sdp_to_live_talking() -> None:
    requests = []
    client = build_client(recording_handler(requests))

    client.post(
        "/api/v1/avatar/webrtc/offer",
        json={"sdp": "browser-offer-sdp", "type": "offer"},
    )

    assert paths_of(requests) == ["/offer"]
    assert json.loads(requests[0].content)["sdp"] == "browser-offer-sdp"


def test_webrtc_offer_returns_answer_and_session_id() -> None:
    client = build_client(recording_handler([]))

    response = client.post(
        "/api/v1/avatar/webrtc/offer",
        json={"sdp": "browser-offer-sdp", "type": "offer"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "sdp": "mock-answer-sdp",
        "type": "answer",
        "session_id": SESSION_ID,
    }


def test_webrtc_offer_rejects_empty_sdp() -> None:
    client = build_client(recording_handler([]))

    response = client.post(
        "/api/v1/avatar/webrtc/offer", json={"sdp": "", "type": "offer"}
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------
# 会话解析
# --------------------------------------------------------------------------


def test_say_falls_back_to_live_talking_when_no_local_session() -> None:
    """本地没有会话时反查后端——这是 MediaMTX 拉流场景下的主路径。

    必须选「最后创建的那个」：后端按创建顺序返回，MediaMTX 的会话总是最新。
    """
    requests = []
    client = build_client(
        recording_handler(
            requests,
            {
                "/api/admin/sessions": {
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "sessions": [
                            {"sessionid": SESSION_ID},
                            {"sessionid": NEWER_SESSION_ID},
                        ]
                    },
                }
            },
        )
    )

    response = client.post("/api/v1/chat/say", json={"text": "你好"})

    assert paths_of(requests) == ["/api/admin/sessions", "/human", "/is_speaking"]
    assert json.loads(requests[1].content)["sessionid"] == NEWER_SESSION_ID
    assert response.json()["session_id"] == NEWER_SESSION_ID


def test_say_reuses_locally_cached_session_without_querying() -> None:
    """本地已有会话时不该再去查一遍后端——那会把开销加到每次调用上。"""
    requests = []
    client = build_client(recording_handler(requests))
    connect_via_offer(client)
    requests.clear()

    client.post("/api/v1/chat/say", json={"text": "你好"})

    assert "/api/admin/sessions" not in paths_of(requests)


def test_say_without_any_session_is_rejected() -> None:
    requests = []
    client = build_client(
        recording_handler(requests, {"/api/admin/sessions": {"code": 0, "msg": "ok", "data": {"sessions": []}}})
    )

    response = client.post("/api/v1/chat/say", json={"text": "你好"})

    assert response.status_code == 409
    assert response.json()["detail"] == "No active avatar session on LiveTalking"
    # 关键：没有会话就不该向 LiveTalking 发文本
    assert "/human" not in paths_of(requests)


# --------------------------------------------------------------------------
# 发送文本
# --------------------------------------------------------------------------


def test_say_forwards_echo_type_with_session_id() -> None:
    """浏览器只发文本，session_id 由本地 FastAPI 自己决定。"""
    requests = []
    client = build_client(recording_handler(requests))

    response = client.post("/api/v1/chat/say", json={"text": "请你介绍一下今天的课程"})

    assert response.status_code == 200
    human = next(r for r in requests if r.url.path == "/human")
    payload = json.loads(human.content)
    assert payload["sessionid"] == SESSION_ID
    assert payload["text"] == "请你介绍一下今天的课程"
    assert payload["interrupt"] is False
    # 架构边界：绝不能让云端替我们生成内容
    assert payload["type"] == "echo"
    assert payload["type"] != "chat"


def test_say_reports_verified_speaking() -> None:
    client = build_client(recording_handler([]))

    response = client.post("/api/v1/chat/say", json={"text": "你好"})

    body = response.json()
    assert body["status"] == "accepted"
    assert body["verified_speaking"] is True


def test_say_reports_unverified_when_speaking_never_observed() -> None:
    """受理 ≠ 说过。没观察到播报时必须如实标注，而不是照报成功。"""
    client = build_client(
        recording_handler([], {"/is_speaking": {"code": 0, "msg": "ok", "data": False}})
    )

    response = client.post("/api/v1/chat/say", json={"text": "你好"})

    assert response.status_code == 200
    assert response.json()["status"] == "accepted_unverified"
    assert response.json()["verified_speaking"] is False


def test_say_reports_unverified_when_session_missing_at_verify_time() -> None:
    """会话在验证时已消失，也应标为未确认，而不是抛异常。"""
    client = build_client(
        recording_handler([], {"/is_speaking": {"code": -1, "msg": "session not found"}})
    )

    response = client.post("/api/v1/chat/say", json={"text": "你好"})

    assert response.json()["verified_speaking"] is False


def test_say_response_does_not_claim_completion() -> None:
    """响应只能是「已受理」，不能出现「已完成」这类断言。"""
    client = build_client(recording_handler([]))

    status = client.post("/api/v1/chat/say", json={"text": "你好"}).json()["status"]

    assert status in {"accepted", "accepted_unverified"}
    assert "completed" not in status and "finished" not in status


# --------------------------------------------------------------------------
# 打断
# --------------------------------------------------------------------------


def test_interrupt_forwards_session_id() -> None:
    requests = []
    client = build_client(recording_handler(requests))
    connect_via_offer(client)
    requests.clear()

    response = client.post("/api/v1/chat/interrupt")

    assert response.status_code == 200
    assert paths_of(requests) == ["/interrupt_talk"]
    assert json.loads(requests[0].content)["sessionid"] == SESSION_ID


def test_interrupt_without_session_is_rejected() -> None:
    requests = []
    client = build_client(
        recording_handler(requests, {"/api/admin/sessions": {"code": 0, "msg": "ok", "data": {"sessions": []}}})
    )

    response = client.post("/api/v1/chat/interrupt")

    assert response.status_code == 409
    assert response.json()["detail"] == "No active avatar session on LiveTalking"
    assert requests and requests[0].url.path == "/api/admin/sessions"


# --------------------------------------------------------------------------
# LiveTalking 出问题时，浏览器应看到可读错误
# --------------------------------------------------------------------------


def test_live_talking_unavailable_returns_502() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = build_client(unreachable)

    response = client.post(
        "/api/v1/avatar/webrtc/offer",
        json={"sdp": "browser-offer-sdp", "type": "offer"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "LiveTalking service unavailable"


def test_live_talking_error_status_returns_502() -> None:
    """协商成功、但后续调用失败时，也应转成 502 而不是抛 traceback。"""

    def failing_on_human(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/offer":
            return httpx.Response(200, json=OFFER_ANSWER)
        return httpx.Response(500, text="internal error")

    client = build_client(failing_on_human)
    connect_via_offer(client)

    response = client.post("/api/v1/chat/say", json={"text": "你好"})

    assert response.status_code == 502
    assert response.json()["detail"] == "LiveTalking returned an error (HTTP 500)"


# --------------------------------------------------------------------------
# /ask —— 本地模型生成回答，然后交给数字人念
# --------------------------------------------------------------------------


def test_ask_generates_answer_then_speaks_it() -> None:
    """完整链路：问题进模型，模型的回答进 LiveTalking。

    关键是送给 /human 的必须是**回答**而不是问题——否则数字人会把用户的
    问题原样念一遍，看起来像是"复读"，而不是"回答"。
    """
    requests = []
    llm = StubLLM()
    speaking_states = iter([False, True])
    client = build_client(
        recording_handler(
            requests,
            {
                "/is_speaking": lambda: {
                    "code": 0,
                    "msg": "ok",
                    "data": next(speaking_states),
                }
            },
        ),
        llm=llm,
    )

    response = client.post("/api/v1/chat/ask", json={"text": QUESTION})

    assert response.status_code == 200
    body = response.json()
    assert body["question"] == QUESTION
    assert body["answer"] == MODEL_ANSWER
    assert body["status"] == "accepted"
    assert body["verified_speaking"] is True
    assert body["metrics"]["llm_ms"] >= 0
    assert body["metrics"]["avatar_startup_ms"] is not None
    assert body["metrics"]["avatar_startup_ms"] >= 0
    assert body["metrics"]["avatar_startup_status"] == "measured"

    assert llm.calls == [[{"role": "user", "content": QUESTION}]]
    human = next(r for r in requests if r.url.path == "/human")
    payload = json.loads(human.content)
    assert payload["text"] == MODEL_ANSWER
    assert payload["type"] == "echo"


def test_ask_does_not_call_live_talking_when_model_fails() -> None:
    """模型失败就不该去打扰数字人——否则数字人会念出半截或空内容。"""
    requests = []
    llm = StubLLM(error=LLMBadResponse("模型返回了空回答"))
    client = build_client(recording_handler(requests), llm=llm)

    response = client.post("/api/v1/chat/ask", json={"text": QUESTION})

    assert response.status_code == 502
    assert response.json()["detail"] == "Ollama returned an error"
    assert "/human" not in paths_of(requests)


def test_ask_returns_502_when_model_unavailable() -> None:
    requests = []
    llm = StubLLM(error=LLMUnavailable("无法连接 Ollama"))
    client = build_client(recording_handler(requests), llm=llm)

    response = client.post("/api/v1/chat/ask", json={"text": QUESTION})

    assert response.status_code == 502
    assert response.json()["detail"] == "Ollama service unavailable"
    assert "/human" not in paths_of(requests)


def test_ask_checks_session_before_spending_a_generation() -> None:
    """没有可播报的会话时应当直接拒绝，而不是白跑一次模型推理。"""
    llm = StubLLM()
    client = build_client(
        recording_handler(
            [], {"/api/admin/sessions": {"code": 0, "msg": "ok", "data": {"sessions": []}}}
        ),
        llm=llm,
    )

    response = client.post("/api/v1/chat/ask", json={"text": QUESTION})

    assert response.status_code == 409
    assert llm.calls == []


def test_ask_skips_speech_when_interrupted_during_generation() -> None:
    """生成期间被打断：回答照给，但不能接着播已经过时的那句话。"""
    requests = []
    # on_chat 在「模型生成中」这一刻递增打断计数，模拟用户此刻点了打断
    llm = StubLLM(on_chat=chat._bump_interrupt_epoch)
    client = build_client(recording_handler(requests), llm=llm)

    response = client.post("/api/v1/chat/ask", json={"text": QUESTION})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["answer"] == MODEL_ANSWER
    assert body["verified_speaking"] is False
    assert body["metrics"]["llm_ms"] >= 0
    assert body["metrics"]["avatar_startup_ms"] is None
    assert body["metrics"]["avatar_startup_status"] == "cancelled"
    assert "/human" not in paths_of(requests)


def test_ask_reports_unverified_when_speaking_never_observed() -> None:
    client = build_client(
        recording_handler([], {"/is_speaking": {"code": 0, "msg": "ok", "data": False}})
    )

    response = client.post("/api/v1/chat/ask", json={"text": QUESTION})

    body = response.json()
    assert body["status"] == "accepted_unverified"
    assert body["verified_speaking"] is False
    assert body["metrics"]["avatar_startup_ms"] is None
    assert body["metrics"]["avatar_startup_status"] == "not_observed"


def test_ask_does_not_measure_new_start_when_avatar_is_already_speaking() -> None:
    """上一轮尚在播报时，当前 true 不能冒充本轮起播转换。"""
    requests = []
    client = build_client(recording_handler(requests))

    response = client.post("/api/v1/chat/ask", json={"text": QUESTION})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["verified_speaking"] is True
    assert body["metrics"]["llm_ms"] >= 0
    assert body["metrics"]["avatar_startup_ms"] is None
    assert body["metrics"]["avatar_startup_status"] == "already_speaking"
    assert "/human" in paths_of(requests)


def test_ask_keeps_working_when_metrics_baseline_is_unavailable() -> None:
    """指标基线查询失败不得改变原有播报成功语义。"""
    requests = []
    speaking_calls = 0
    llm = StubLLM()

    def baseline_fails_once(request: httpx.Request) -> httpx.Response:
        nonlocal speaking_calls
        requests.append(request)
        if request.url.path == "/is_speaking":
            speaking_calls += 1
            if speaking_calls == 1:
                return httpx.Response(500, text="metrics unavailable")
        return httpx.Response(
            200,
            json=DEFAULT_RESPONSES.get(
                request.url.path, {"code": 0, "msg": "ok", "data": {}}
            ),
        )

    client = build_client(baseline_fails_once, llm=llm)

    response = client.post("/api/v1/chat/ask", json={"text": QUESTION})
    next_response = client.post("/api/v1/chat/ask", json={"text": "继续解释。"})

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == MODEL_ANSWER
    assert body["status"] == "accepted"
    assert body["verified_speaking"] is True
    assert body["metrics"]["avatar_startup_ms"] is None
    assert body["metrics"]["avatar_startup_status"] == "unavailable"
    assert "/human" in paths_of(requests)
    assert next_response.status_code == 200
    assert llm.calls[1] == [
        {"role": "user", "content": QUESTION},
        {"role": "assistant", "content": MODEL_ANSWER},
        {"role": "user", "content": "继续解释。"},
    ]


def test_ask_second_round_receives_first_successful_turn() -> None:
    llm = SequencedLLM(
        ["RAG 是检索增强生成。", "它先检索可靠资料，再基于资料生成回答。"]
    )
    client = build_client(recording_handler([]), llm=llm)

    first = client.post("/api/v1/chat/ask", json={"text": "什么是 RAG？"})
    second = client.post("/api/v1/chat/ask", json={"text": "那它为什么更可靠？"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert llm.calls[1] == [
        {"role": "user", "content": "什么是 RAG？"},
        {"role": "assistant", "content": "RAG 是检索增强生成。"},
        {"role": "user", "content": "那它为什么更可靠？"},
    ]


def test_ask_history_trims_only_after_sixth_turn_commits() -> None:
    llm = SequencedLLM([f"A{index}" for index in range(1, 8)])
    client = build_client(recording_handler([]), llm=llm)

    for index in range(1, 7):
        response = client.post("/api/v1/chat/ask", json={"text": f"Q{index}"})
        assert response.status_code == 200

    expected_sixth_call = []
    for index in range(1, 6):
        expected_sixth_call.extend(
            [
                {"role": "user", "content": f"Q{index}"},
                {"role": "assistant", "content": f"A{index}"},
            ]
        )
    expected_sixth_call.append({"role": "user", "content": "Q6"})
    assert llm.calls[5] == expected_sixth_call

    stored_after_sixth = chat.conversation_store.get_messages(SESSION_ID)
    assert stored_after_sixth[0] == {"role": "user", "content": "Q2"}
    assert stored_after_sixth[-1] == {"role": "assistant", "content": "A6"}

    seventh = client.post("/api/v1/chat/ask", json={"text": "Q7"})

    assert seventh.status_code == 200
    assert {"role": "user", "content": "Q1"} not in llm.calls[6]
    assert {"role": "assistant", "content": "A1"} not in llm.calls[6]
    assert llm.calls[6][0] == {"role": "user", "content": "Q2"}
    assert llm.calls[6][-1] == {"role": "user", "content": "Q7"}


def test_ask_llm_failure_does_not_pollute_history() -> None:
    llm = SequencedLLM(["A1", LLMBadResponse("生成失败"), "A3"])
    client = build_client(recording_handler([]), llm=llm)

    assert client.post("/api/v1/chat/ask", json={"text": "Q1"}).status_code == 200
    assert client.post("/api/v1/chat/ask", json={"text": "Q2"}).status_code == 502
    assert client.post("/api/v1/chat/ask", json={"text": "Q3"}).status_code == 200

    assert llm.calls[2] == [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q3"},
    ]


def test_ask_live_talking_failure_does_not_pollute_history() -> None:
    requests = []
    human_calls = 0

    def fail_second_human(request: httpx.Request) -> httpx.Response:
        nonlocal human_calls
        requests.append(request)
        if request.url.path == "/human":
            human_calls += 1
            if human_calls == 2:
                return httpx.Response(500, text="avatar unavailable")
        return httpx.Response(
            200,
            json=DEFAULT_RESPONSES.get(
                request.url.path, {"code": 0, "msg": "ok", "data": {}}
            ),
        )

    llm = SequencedLLM(["A1", "A2", "A3"])
    client = build_client(fail_second_human, llm=llm)

    assert client.post("/api/v1/chat/ask", json={"text": "Q1"}).status_code == 200
    assert client.post("/api/v1/chat/ask", json={"text": "Q2"}).status_code == 502
    assert client.post("/api/v1/chat/ask", json={"text": "Q3"}).status_code == 200

    assert llm.calls[2] == [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q3"},
    ]


def test_ask_cancelled_turn_does_not_pollute_history() -> None:
    def interrupt_second_call(call_number: int) -> None:
        if call_number == 2:
            chat._bump_interrupt_epoch()

    llm = SequencedLLM(["A1", "A2", "A3"], on_call=interrupt_second_call)
    client = build_client(recording_handler([]), llm=llm)

    assert client.post("/api/v1/chat/ask", json={"text": "Q1"}).status_code == 200
    cancelled = client.post("/api/v1/chat/ask", json={"text": "Q2"})
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert client.post("/api/v1/chat/ask", json={"text": "Q3"}).status_code == 200

    assert llm.calls[2] == [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q3"},
    ]


def test_ask_accepted_unverified_turn_is_committed() -> None:
    llm = SequencedLLM(["A1", "A2"])
    client = build_client(
        recording_handler([], {"/is_speaking": {"code": 0, "msg": "ok", "data": False}}),
        llm=llm,
    )

    first = client.post("/api/v1/chat/ask", json={"text": "Q1"})
    second = client.post("/api/v1/chat/ask", json={"text": "Q2"})

    assert first.json()["status"] == "accepted_unverified"
    assert second.status_code == 200
    assert llm.calls[1] == [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2"},
    ]


def test_ask_serializes_concurrent_requests_for_same_session() -> None:
    class BlockingFirstLLM(LLMService):
        def __init__(self) -> None:
            self.calls: list[list[ChatMessage]] = []
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def chat(self, messages: Sequence[ChatMessage]) -> str:
            self.calls.append(
                [
                    {"role": message["role"], "content": message["content"]}
                    for message in messages
                ]
            )
            call_number = len(self.calls)
            if call_number == 1:
                self.first_started.set()
                await self.release_first.wait()
            return f"A{call_number}"

    requests = []
    service = LiveTalkingService(
        base_url=BASE_URL,
        transport=httpx.MockTransport(recording_handler(requests)),
    )
    llm = BlockingFirstLLM()
    set_session_id(SESSION_ID)

    async def run_concurrently() -> None:
        first = asyncio.create_task(
            chat.chat_ask(ChatAskRequest(text="Q1"), service=service, llm=llm)
        )
        await llm.first_started.wait()
        second = asyncio.create_task(
            chat.chat_ask(ChatAskRequest(text="Q2"), service=service, llm=llm)
        )
        await asyncio.sleep(0)
        assert len(llm.calls) == 1
        llm.release_first.set()
        await asyncio.gather(first, second)

    asyncio.run(run_concurrently())

    assert llm.calls == [
        [{"role": "user", "content": "Q1"}],
        [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
        ],
    ]


def test_reset_clears_history_before_next_question() -> None:
    llm = SequencedLLM(["A1", "A2"])
    client = build_client(recording_handler([]), llm=llm)

    first = client.post("/api/v1/chat/ask", json={"text": "Q1"})
    reset = client.post("/api/v1/chat/reset")
    second = client.post("/api/v1/chat/ask", json={"text": "Q2"})

    assert first.status_code == 200
    assert reset.status_code == 200
    assert reset.json() == {"status": "reset", "session_id": SESSION_ID}
    assert second.status_code == 200
    assert llm.calls[1] == [{"role": "user", "content": "Q2"}]


def test_reset_is_idempotent_for_empty_history() -> None:
    client = build_client(recording_handler([]))

    first = client.post("/api/v1/chat/reset")
    second = client.post("/api/v1/chat/reset")

    assert first.status_code == 200
    assert first.json() == {"status": "reset", "session_id": SESSION_ID}
    assert second.status_code == 200
    assert second.json() == first.json()


def test_reset_waits_for_in_flight_ask_then_clears_committed_turn() -> None:
    class BlockingLLM(LLMService):
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def chat(self, messages: Sequence[ChatMessage]) -> str:
            assert messages == [{"role": "user", "content": "Q1"}]
            self.started.set()
            await self.release.wait()
            return "A1"

    service = LiveTalkingService(
        base_url=BASE_URL,
        transport=httpx.MockTransport(recording_handler([])),
    )
    llm = BlockingLLM()
    set_session_id(SESSION_ID)

    async def ask_then_reset() -> None:
        ask_task = asyncio.create_task(
            chat.chat_ask(ChatAskRequest(text="Q1"), service=service, llm=llm)
        )
        await llm.started.wait()

        reset_task = asyncio.create_task(chat.chat_reset(service=service))
        await asyncio.sleep(0)
        assert not reset_task.done()

        llm.release.set()
        ask_response, reset_response = await asyncio.gather(ask_task, reset_task)
        assert ask_response.status == "accepted"
        assert reset_response.status == "reset"

    asyncio.run(ask_then_reset())

    assert chat.conversation_store.get_messages(SESSION_ID) == []


def test_ask_rejects_empty_question() -> None:
    client = build_client(recording_handler([]))

    assert client.post("/api/v1/chat/ask", json={"text": ""}).status_code == 422


def test_interrupt_still_works_alongside_ask() -> None:
    """V0.1 的打断不能被 V0.2 破坏。"""
    requests = []
    client = build_client(recording_handler(requests))
    connect_via_offer(client)
    requests.clear()

    response = client.post("/api/v1/chat/interrupt")

    assert response.status_code == 200
    assert paths_of(requests) == ["/interrupt_talk"]
