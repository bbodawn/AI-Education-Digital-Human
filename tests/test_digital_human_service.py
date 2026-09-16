"""验证 LiveTalkingService 与 LiveTalking HTTP 契约的对齐。

这一层唯一的职责，就是把本地意图准确翻译成 LiveTalking 的 API 调用，并把失败
归纳成上层能理解的错误类型。翻译错了不会当场报错，而是等到真实联调时以
「数字人不说话」这种难以定位的形式暴露出来。所以这里用 httpx.MockTransport
拦下真实网络，断言发出去的请求本身长什么样——不发真实请求，也不要求
LiveTalking 正在运行。
"""

import asyncio
import json

import httpx
import pytest

from app.services.digital_human import (
    DigitalHumanBadResponse,
    DigitalHumanUnavailable,
    LiveTalkingService,
    WebRTCSession,
)

BASE_URL = "http://127.0.0.1:8010"
SESSION_ID = 7
OFFER_ANSWER = {"sdp": "mock-answer-sdp", "type": "answer", "sessionid": SESSION_ID}


def build_service(recorder, base_url=BASE_URL, responses=None, raise_error=None):
    """构造一个会记录请求、但不做真实网络调用的 LiveTalkingService。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if raise_error is not None:
            raise raise_error
        recorder.append(request)
        body = (responses or {}).get(
            request.url.path, {"code": 0, "msg": "ok", "data": {}}
        )
        return httpx.Response(200, json=body)

    return LiveTalkingService(base_url=base_url, transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------
# negotiate_webrtc —— 远端会话正是在这一步建立的
# --------------------------------------------------------------------------


def test_negotiate_webrtc_posts_browser_sdp_to_offer_endpoint() -> None:
    requests = []
    service = build_service(requests, responses={"/offer": OFFER_ANSWER})

    asyncio.run(service.negotiate_webrtc(sdp="browser-offer-sdp", sdp_type="offer"))

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/offer"
    payload = json.loads(requests[0].content)
    assert payload["sdp"] == "browser-offer-sdp"
    assert payload["type"] == "offer"


def test_negotiate_webrtc_returns_answer_and_session_id() -> None:
    """LiveTalking 的应答里 session_id 与 sdp answer 是并列返回的。"""
    requests = []
    service = build_service(
        requests,
        responses={"/offer": {"sdp": "mock-answer-sdp", "type": "answer", "sessionid": SESSION_ID}},
    )

    session = asyncio.run(service.negotiate_webrtc(sdp="browser-offer-sdp", sdp_type="offer"))

    assert isinstance(session, WebRTCSession)
    assert session.sdp == "mock-answer-sdp"
    assert session.type == "answer"
    assert session.session_id == SESSION_ID


def test_negotiate_webrtc_raises_when_session_id_missing() -> None:
    """应答缺少 sessionid 时必须当场失败。

    否则 None 会一路传到浏览器，问题要到「数字人没反应」时才暴露，
    届时很难定位到是这里。
    """
    requests = []
    service = build_service(requests, responses={"/offer": {"sdp": "mock-answer-sdp"}})

    with pytest.raises(DigitalHumanBadResponse):
        asyncio.run(service.negotiate_webrtc(sdp="browser-offer-sdp", sdp_type="offer"))


# --------------------------------------------------------------------------
# say / interrupt
# --------------------------------------------------------------------------


def test_say_posts_to_human_endpoint() -> None:
    requests = []
    service = build_service(requests)

    asyncio.run(service.say(session_id=SESSION_ID, text="什么是光合作用？"))

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/human"


def test_say_uses_echo_type_not_chat() -> None:
    """type 必须是 echo。

    echo = 原样朗读本地给出的文本；chat = 让 LiveTalking 在云端自行调用 LLM
    生成回答。本项目的 Qwen 必须留在本地，这条断言守的是架构边界：
    一旦被改成 chat，云端就会绕开本地业务逻辑自己产生内容。
    """
    requests = []
    service = build_service(requests)

    asyncio.run(service.say(session_id=SESSION_ID, text="什么是光合作用？"))

    payload = json.loads(requests[0].content)
    assert payload["type"] == "echo"
    assert payload["type"] != "chat"


def test_say_carries_session_id_text_and_interrupt_flag() -> None:
    requests = []
    service = build_service(requests)

    asyncio.run(service.say(session_id=SESSION_ID, text="你好", interrupt=True))

    payload = json.loads(requests[0].content)
    assert payload["sessionid"] == SESSION_ID
    assert payload["text"] == "你好"
    assert payload["interrupt"] is True


def test_interrupt_posts_to_interrupt_talk() -> None:
    requests = []
    service = build_service(requests)

    asyncio.run(service.interrupt(session_id=SESSION_ID))

    assert requests[0].url.path == "/interrupt_talk"
    assert json.loads(requests[0].content)["sessionid"] == SESSION_ID


def test_requests_go_to_configured_base_url() -> None:
    """请求地址完全由 base_url 决定。

    这正是 V0.1 的架构目标：同一份代码，只改 AVATAR_BASE_URL，
    就能从「数字人跑在本机」切到「数字人跑在云端 GPU」。
    """
    requests = []
    service = build_service(requests, base_url="http://10.0.0.5:8010")

    asyncio.run(service.say(session_id=SESSION_ID, text="你好"))

    assert requests[0].url.host == "10.0.0.5"
    assert requests[0].url.port == 8010


# --------------------------------------------------------------------------
# 错误契约 —— 上层不该看见 httpx 的异常类型
# --------------------------------------------------------------------------


def test_unreachable_live_talking_raises_unavailable() -> None:
    """LiveTalking 没启动时，应当是 DigitalHumanUnavailable（映射为 502）。"""
    service = build_service([], raise_error=httpx.ConnectError("connection refused"))

    with pytest.raises(DigitalHumanUnavailable):
        asyncio.run(service.say(session_id=SESSION_ID, text="你好"))


def test_live_talking_error_status_raises_bad_response() -> None:
    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    failing = LiveTalkingService(
        base_url=BASE_URL, transport=httpx.MockTransport(failing_handler)
    )

    with pytest.raises(DigitalHumanBadResponse) as excinfo:
        asyncio.run(failing.say(session_id=SESSION_ID, text="你好"))

    assert excinfo.value.status_code == 500


def test_list_sessions_returns_sessions_in_backend_order() -> None:
    """反查会话时必须保留后端给的顺序。

    调用方靠「最后一个 = 最近创建的」来定位 MediaMTX 建立的会话，
    这里重排就等于把那个判据毁掉。
    """
    requests = []
    service = build_service(
        requests,
        responses={
            "/api/admin/sessions": {
                "code": 0,
                "msg": "ok",
                "data": {"sessions": [{"sessionid": "old"}, {"sessionid": "new"}]},
            }
        },
    )

    sessions = asyncio.run(service.list_sessions())

    assert [s["sessionid"] for s in sessions] == ["old", "new"]
    assert requests[0].url.path == "/api/admin/sessions"
    assert requests[0].method == "GET"


def test_list_sessions_returns_empty_list_when_backend_shape_unexpected() -> None:
    """后端返回格式不对时给空列表，而不是把 None 抛给调用方。"""
    service = build_service([], responses={"/api/admin/sessions": {"code": 0, "data": {}}})

    assert asyncio.run(service.list_sessions()) == []


def test_is_speaking_reads_boolean_from_data() -> None:
    requests = []
    service = build_service(
        requests, responses={"/is_speaking": {"code": 0, "msg": "ok", "data": True}}
    )

    assert asyncio.run(service.is_speaking(session_id=SESSION_ID)) is True
    assert json.loads(requests[0].content)["sessionid"] == SESSION_ID


def test_is_speaking_returns_none_when_session_missing() -> None:
    """会话不存在时返回 None，而不是 False。

    LiveTalking 用 {code:-1} + HTTP 200 表达「会话不存在」。若把它当成
    False，调用方就会以为「会话在、只是没说话」，从而静默地用错会话。
    """
    service = build_service(
        [], responses={"/is_speaking": {"code": -1, "msg": "session not found"}}
    )

    assert asyncio.run(service.is_speaking(session_id="nope")) is None


def test_http_client_ignores_system_proxy() -> None:
    """必须绕开系统代理。

    Windows 上通常开着系统代理（Clash / 企业 VPN 会写进注册表），而 httpx 默认
    trust_env=True 会据此把请求送进代理——哪怕目标是 127.0.0.1，且 ProxyOverride
    里明明写着 127.* 白名单（httpx 不认它）。后果是连不上 LiveTalking 时拿回一个
    由代理产生的 502，错误被误报成「LiveTalking 返回错误」而非「连不上」。
    实测联调中确实撞到了这一点，这条断言把修复锁住。
    """
    client = LiveTalkingService(base_url=BASE_URL)._build_client()
    asyncio.run(client.aclose())

    assert client.trust_env is False
