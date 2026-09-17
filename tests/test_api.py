"""验证本地 FastAPI 的对外接口。

用 httpx.MockTransport 伪造 LiveTalking，因此不需要真的启动它。
断言的重点是四条边界：

  1. 浏览器只跟本地 FastAPI 说话（相对路径、不出现 LiveTalking 地址）
  2. FastAPI 转译给 LiveTalking 的内容正确（尤其是 type=echo）
  3. 会话不是本地创建的时，能反查出来并且选哪个是可见的
  4. LiveTalking 出问题时，浏览器收到可读的 JSON 而不是 Python traceback
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import chat
from app.main import app
from app.services import get_llm_service, get_service
from app.services.digital_human import LiveTalkingService
from app.services.llm import LLMBadResponse, LLMService, LLMUnavailable
from app.session import clear_session_id

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
        self.questions: list[str] = []

    async def chat(self, text: str) -> str:
        self.questions.append(text)
        if self.on_chat is not None:
            self.on_chat()
        if self.error is not None:
            raise self.error
        return self.answer

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
    monkeypatch.setattr(chat, "_SPEAKING_POLL_ATTEMPTS", 2)
    monkeypatch.setattr(chat, "_SPEAKING_POLL_INTERVAL", 0)
    # 打断计数是模块级全局，不清零会让上一个用例的打断影响到下一个
    monkeypatch.setattr(chat, "_interrupt_epoch", 0)
    yield
    clear_session_id()
    app.dependency_overrides.clear()


def build_client(handler, llm: StubLLM | None = None) -> TestClient:
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

    assert llm.questions == [QUESTION]
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
    assert llm.questions == []


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

    client = build_client(baseline_fails_once)

    response = client.post("/api/v1/chat/ask", json={"text": QUESTION})

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == MODEL_ANSWER
    assert body["status"] == "accepted"
    assert body["verified_speaking"] is True
    assert body["metrics"]["avatar_startup_ms"] is None
    assert body["metrics"]["avatar_startup_status"] == "unavailable"
    assert "/human" in paths_of(requests)


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
