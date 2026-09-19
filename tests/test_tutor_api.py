"""Tutor API 的事务提交、失败语义和共享锁测试。"""

import asyncio
import json
from collections.abc import Sequence

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import chat, tutor as tutor_api
from app.conversation import ChatMessage, ConversationStore
from app.main import app
from app.schemas import TutorAnswerRequest
from app.services import get_llm_service, get_service
from app.services.digital_human import LiveTalkingService
from app.services.llm import LLMBadResponse, LLMService
from app.session import clear_session_id, set_session_id
from app.tutor import TutorSessionStore, TutorState


BASE_URL = "http://127.0.0.1:8010"
SESSION_ID = "tutor-session"
TOPIC = "RAG 基础"
FIRST_QUESTION = "为什么企业知识库问答通常使用 RAG？"
USER_ANSWER = "因为它可以检索可信的外部知识。"
RAW_FEEDBACK = (
    "评价：回答基本正确。\n"
    "解释：RAG 会先检索外部知识，再基于资料生成回答。\n"
    "下一题：Retriever 在 RAG 中负责什么？"
)
NEXT_QUESTION = "Retriever 在 RAG 中负责什么？"


DEFAULT_RESPONSES = {
    "/api/admin/sessions": {
        "code": 0,
        "msg": "ok",
        "data": {"sessions": [{"sessionid": SESSION_ID}]},
    },
    "/is_speaking": {"code": 0, "msg": "ok", "data": True},
    "/human": {"code": 0, "msg": "ok", "data": {}},
}


class TutorLLM(LLMService):
    """按顺序返回结果并记录 messages/instruction 的模型替身。"""

    def __init__(self, outcomes, on_call=None) -> None:
        self.outcomes = list(outcomes)
        self.on_call = on_call
        self.calls: list[tuple[list[ChatMessage], str | None]] = []

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        instruction: str | None = None,
    ) -> str:
        self.calls.append(
            (
                [
                    {"role": message["role"], "content": message["content"]}
                    for message in messages
                ],
                instruction,
            )
        )
        call_number = len(self.calls)
        if self.on_call is not None:
            self.on_call(call_number)
        outcome = self.outcomes[call_number - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def _isolated_tutor_state(monkeypatch):
    clear_session_id()
    monkeypatch.setattr(chat, "conversation_store", ConversationStore())
    monkeypatch.setattr(tutor_api, "tutor_session_store", TutorSessionStore())
    monkeypatch.setattr(chat, "_SPEAKING_POLL_ATTEMPTS", 2)
    monkeypatch.setattr(chat, "_SPEAKING_POLL_INTERVAL", 0)
    monkeypatch.setattr(chat, "_interrupt_epoch", 0)
    yield
    clear_session_id()
    app.dependency_overrides.clear()


def recording_handler(recorder, responses=None):
    merged = {**DEFAULT_RESPONSES, **(responses or {})}

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        response = merged.get(request.url.path, {"code": 0, "msg": "ok", "data": {}})
        if callable(response):
            response = response()
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    return handler


def build_client(handler, llm: LLMService) -> TestClient:
    service = LiveTalkingService(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    app.dependency_overrides[get_service] = lambda: service
    app.dependency_overrides[get_llm_service] = lambda: llm
    return TestClient(app)


def request_paths(requests) -> list[str]:
    return [request.url.path for request in requests]


def seed_tutor_state() -> TutorState:
    state = TutorState(
        mode="guided_qa",
        topic=TOPIC,
        current_question=FIRST_QUESTION,
        question_index=1,
    )
    tutor_api.tutor_session_store.set(SESSION_ID, state)
    chat.conversation_store.append_turn(
        SESSION_ID,
        f"开始学习主题：{TOPIC}",
        FIRST_QUESTION,
    )
    return state


def test_tutor_start_success_commits_state_history_and_instruction() -> None:
    requests = []
    llm = TutorLLM([f"  {FIRST_QUESTION}  "])
    client = build_client(recording_handler(requests), llm)

    response = client.post(
        "/api/v1/tutor/start",
        json={"topic": f"  {TOPIC}  ", "mode": "guided_qa"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["session_id"] == SESSION_ID
    assert body["topic"] == TOPIC
    assert body["question"] == FIRST_QUESTION
    assert body["question_index"] == 1
    assert body["metrics"]["llm_ms"] >= 0
    assert llm.calls[0][0] == [
        {"role": "user", "content": f"开始学习主题：{TOPIC}"}
    ]
    assert "学习主题：RAG 基础" in llm.calls[0][1]
    assert "当前题号：1" in llm.calls[0][1]
    assert tutor_api.tutor_session_store.get(SESSION_ID) == TutorState(
        mode="guided_qa",
        topic=TOPIC,
        current_question=FIRST_QUESTION,
        question_index=1,
    )
    assert chat.conversation_store.get_messages(SESSION_ID) == [
        {"role": "user", "content": f"开始学习主题：{TOPIC}"},
        {"role": "assistant", "content": FIRST_QUESTION},
    ]
    human = next(request for request in requests if request.url.path == "/human")
    assert json.loads(human.content)["text"] == FIRST_QUESTION


@pytest.mark.parametrize(
    "payload",
    [
        {"topic": ""},
        {"topic": "   "},
        {"topic": TOPIC, "mode": "unsupported"},
    ],
)
def test_tutor_start_validates_topic_and_mode(payload) -> None:
    llm = TutorLLM([FIRST_QUESTION])
    client = build_client(recording_handler([]), llm)

    response = client.post("/api/v1/tutor/start", json=payload)

    assert response.status_code == 422
    assert llm.calls == []


def test_tutor_start_empty_question_does_not_commit_or_speak() -> None:
    requests = []
    llm = TutorLLM(["   "])
    client = build_client(recording_handler(requests), llm)

    response = client.post("/api/v1/tutor/start", json={"topic": TOPIC})

    assert response.status_code == 502
    assert tutor_api.tutor_session_store.get(SESSION_ID) is None
    assert chat.conversation_store.get_messages(SESSION_ID) == []
    assert "/human" not in request_paths(requests)


def test_tutor_start_llm_failure_clears_old_context_without_new_commit() -> None:
    old_state = seed_tutor_state()
    llm = TutorLLM([LLMBadResponse("生成失败")])
    client = build_client(recording_handler([]), llm)

    response = client.post("/api/v1/tutor/start", json={"topic": "新主题"})

    assert response.status_code == 502
    assert old_state is not None
    assert tutor_api.tutor_session_store.get(SESSION_ID) is None
    assert chat.conversation_store.get_messages(SESSION_ID) == []


def test_tutor_start_cancelled_does_not_commit_or_speak() -> None:
    requests = []
    llm = TutorLLM([FIRST_QUESTION], on_call=lambda _: chat._bump_interrupt_epoch())
    client = build_client(recording_handler(requests), llm)

    response = client.post("/api/v1/tutor/start", json={"topic": TOPIC})

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["metrics"]["avatar_startup_status"] == "cancelled"
    assert tutor_api.tutor_session_store.get(SESSION_ID) is None
    assert chat.conversation_store.get_messages(SESSION_ID) == []
    assert "/human" not in request_paths(requests)


def test_tutor_start_live_talking_failure_does_not_commit() -> None:
    requests = []
    llm = TutorLLM([FIRST_QUESTION])
    client = build_client(
        recording_handler(requests, {"/human": httpx.Response(500, text="failed")}),
        llm,
    )

    response = client.post("/api/v1/tutor/start", json={"topic": TOPIC})

    assert response.status_code == 502
    assert tutor_api.tutor_session_store.get(SESSION_ID) is None
    assert chat.conversation_store.get_messages(SESSION_ID) == []


def test_tutor_start_accepted_unverified_commits() -> None:
    llm = TutorLLM([FIRST_QUESTION])
    client = build_client(
        recording_handler([], {"/is_speaking": {"code": 0, "msg": "ok", "data": False}}),
        llm,
    )

    response = client.post("/api/v1/tutor/start", json={"topic": TOPIC})

    assert response.json()["status"] == "accepted_unverified"
    assert response.json()["metrics"]["avatar_startup_status"] == "not_observed"
    assert tutor_api.tutor_session_store.get(SESSION_ID) is not None
    assert len(chat.conversation_store.get_messages(SESSION_ID)) == 2


def test_tutor_start_metrics_failure_keeps_business_success() -> None:
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

    client = build_client(baseline_fails_once, TutorLLM([FIRST_QUESTION]))

    response = client.post("/api/v1/tutor/start", json={"topic": TOPIC})

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert response.json()["metrics"]["avatar_startup_status"] == "unavailable"
    assert tutor_api.tutor_session_store.get(SESSION_ID) is not None


def test_tutor_start_confirmation_failure_after_human_still_commits() -> None:
    requests = []
    speaking_calls = 0

    def confirmation_fails(request: httpx.Request) -> httpx.Response:
        nonlocal speaking_calls
        requests.append(request)
        if request.url.path == "/is_speaking":
            speaking_calls += 1
            if speaking_calls == 1:
                return httpx.Response(200, json={"code": 0, "msg": "ok", "data": False})
            return httpx.Response(500, text="confirmation unavailable")
        return httpx.Response(
            200,
            json=DEFAULT_RESPONSES.get(
                request.url.path, {"code": 0, "msg": "ok", "data": {}}
            ),
        )

    client = build_client(confirmation_fails, TutorLLM([FIRST_QUESTION]))

    response = client.post("/api/v1/tutor/start", json={"topic": TOPIC})

    assert response.status_code == 200
    assert response.json()["status"] == "accepted_unverified"
    assert response.json()["metrics"]["avatar_startup_status"] == "unavailable"
    assert "/human" in request_paths(requests)
    assert tutor_api.tutor_session_store.get(SESSION_ID) is not None


def test_tutor_answer_without_state_returns_conflict() -> None:
    llm = TutorLLM([RAW_FEEDBACK])
    client = build_client(recording_handler([]), llm)

    response = client.post("/api/v1/tutor/answer", json={"text": USER_ANSWER})

    assert response.status_code == 409
    assert response.json()["detail"] == "No active tutor session"
    assert llm.calls == []


def test_tutor_answer_success_uses_history_and_advances_state() -> None:
    requests = []
    seed_tutor_state()
    llm = TutorLLM([RAW_FEEDBACK])
    client = build_client(recording_handler(requests), llm)

    response = client.post("/api/v1/tutor/answer", json={"text": USER_ANSWER})

    assert response.status_code == 200
    body = response.json()
    assert body["evaluation"] == "回答基本正确。"
    assert body["explanation"] == "RAG 会先检索外部知识，再基于资料生成回答。"
    assert body["next_question"] == NEXT_QUESTION
    assert body["question_index"] == 2
    assert body["metrics"]["llm_ms"] >= 0
    messages, instruction = llm.calls[0]
    assert messages == [
        {"role": "user", "content": f"开始学习主题：{TOPIC}"},
        {"role": "assistant", "content": FIRST_QUESTION},
        {"role": "user", "content": USER_ANSWER},
    ]
    assert f"学习主题：{TOPIC}" in instruction
    assert f"当前问题：{FIRST_QUESTION}" in instruction
    assert "当前题号：1" in instruction
    assert tutor_api.tutor_session_store.get(SESSION_ID) == TutorState(
        mode="guided_qa",
        topic=TOPIC,
        current_question=NEXT_QUESTION,
        question_index=2,
    )
    assert chat.conversation_store.get_messages(SESSION_ID)[-2:] == [
        {"role": "user", "content": USER_ANSWER},
        {"role": "assistant", "content": RAW_FEEDBACK},
    ]
    human = next(request for request in requests if request.url.path == "/human")
    assert json.loads(human.content)["text"] == RAW_FEEDBACK


def test_tutor_answer_parser_failure_does_not_advance_or_speak() -> None:
    requests = []
    original = seed_tutor_state()
    history_before = chat.conversation_store.get_messages(SESSION_ID)
    client = build_client(recording_handler(requests), TutorLLM(["评价：正确。\n解释：说明。"]))

    response = client.post("/api/v1/tutor/answer", json={"text": USER_ANSWER})

    assert response.status_code == 502
    assert tutor_api.tutor_session_store.get(SESSION_ID) == original
    assert chat.conversation_store.get_messages(SESSION_ID) == history_before
    assert "/human" not in request_paths(requests)


def test_tutor_answer_llm_failure_does_not_advance() -> None:
    original = seed_tutor_state()
    history_before = chat.conversation_store.get_messages(SESSION_ID)
    client = build_client(
        recording_handler([]),
        TutorLLM([LLMBadResponse("生成失败")]),
    )

    response = client.post("/api/v1/tutor/answer", json={"text": USER_ANSWER})

    assert response.status_code == 502
    assert tutor_api.tutor_session_store.get(SESSION_ID) == original
    assert chat.conversation_store.get_messages(SESSION_ID) == history_before


def test_tutor_answer_cancelled_does_not_advance_or_speak() -> None:
    requests = []
    original = seed_tutor_state()
    history_before = chat.conversation_store.get_messages(SESSION_ID)
    llm = TutorLLM([RAW_FEEDBACK], on_call=lambda _: chat._bump_interrupt_epoch())
    client = build_client(recording_handler(requests), llm)

    response = client.post("/api/v1/tutor/answer", json={"text": USER_ANSWER})

    assert response.json()["status"] == "cancelled"
    assert tutor_api.tutor_session_store.get(SESSION_ID) == original
    assert chat.conversation_store.get_messages(SESSION_ID) == history_before
    assert "/human" not in request_paths(requests)


def test_tutor_answer_live_talking_failure_does_not_advance() -> None:
    original = seed_tutor_state()
    history_before = chat.conversation_store.get_messages(SESSION_ID)
    client = build_client(
        recording_handler([], {"/human": httpx.Response(500, text="failed")}),
        TutorLLM([RAW_FEEDBACK]),
    )

    response = client.post("/api/v1/tutor/answer", json={"text": USER_ANSWER})

    assert response.status_code == 502
    assert tutor_api.tutor_session_store.get(SESSION_ID) == original
    assert chat.conversation_store.get_messages(SESSION_ID) == history_before


def test_tutor_answer_accepted_unverified_advances() -> None:
    seed_tutor_state()
    client = build_client(
        recording_handler([], {"/is_speaking": {"code": 0, "msg": "ok", "data": False}}),
        TutorLLM([RAW_FEEDBACK]),
    )

    response = client.post("/api/v1/tutor/answer", json={"text": USER_ANSWER})

    assert response.json()["status"] == "accepted_unverified"
    assert response.json()["metrics"]["avatar_startup_status"] == "not_observed"
    assert tutor_api.tutor_session_store.get(SESSION_ID).question_index == 2
    assert len(chat.conversation_store.get_messages(SESSION_ID)) == 4


def test_tutor_reset_clears_tutor_state_and_conversation_and_is_idempotent() -> None:
    seed_tutor_state()
    client = build_client(recording_handler([]), TutorLLM([]))

    first = client.post("/api/v1/tutor/reset")
    second = client.post("/api/v1/tutor/reset")

    assert first.status_code == 200
    assert first.json() == {"status": "reset", "session_id": SESSION_ID}
    assert second.json() == first.json()
    assert tutor_api.tutor_session_store.get(SESSION_ID) is None
    assert chat.conversation_store.get_messages(SESSION_ID) == []


def test_chat_reset_keeps_tutor_state_while_clearing_conversation() -> None:
    state = seed_tutor_state()
    client = build_client(recording_handler([]), TutorLLM([]))

    response = client.post("/api/v1/chat/reset")

    assert response.status_code == 200
    assert tutor_api.tutor_session_store.get(SESSION_ID) == state
    assert chat.conversation_store.get_messages(SESSION_ID) == []


def test_tutor_reset_waits_for_answer_then_clears_committed_state() -> None:
    class BlockingLLM(LLMService):
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def chat(
            self,
            messages: Sequence[ChatMessage],
            *,
            instruction: str | None = None,
        ) -> str:
            self.started.set()
            await self.release.wait()
            return RAW_FEEDBACK

    seed_tutor_state()
    service = LiveTalkingService(
        base_url=BASE_URL,
        transport=httpx.MockTransport(recording_handler([])),
    )
    llm = BlockingLLM()
    set_session_id(SESSION_ID)

    async def answer_then_reset() -> None:
        answer_task = asyncio.create_task(
            tutor_api.tutor_answer(
                TutorAnswerRequest(text=USER_ANSWER),
                service=service,
                llm=llm,
            )
        )
        await llm.started.wait()

        reset_task = asyncio.create_task(tutor_api.tutor_reset(service=service))
        await asyncio.sleep(0)
        assert not reset_task.done()

        llm.release.set()
        answer_response, reset_response = await asyncio.gather(answer_task, reset_task)
        assert answer_response.status == "accepted"
        assert reset_response.status == "reset"

    asyncio.run(answer_then_reset())

    assert tutor_api.tutor_session_store.get(SESSION_ID) is None
    assert chat.conversation_store.get_messages(SESSION_ID) == []
