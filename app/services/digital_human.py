"""数字人能力的抽象，以及基于 LiveTalking 的实现。

分层的意义：本地业务代码只认识 DigitalHumanService，不认识 LiveTalking。
将来若把 Avatar 后端换成别的东西，只需要在 services 下新增一个实现类。

本层的定位是「无状态的 HTTP 客户端」：它不持有会话，只负责把一次调用
准确翻译成 LiveTalking 的 HTTP 请求。会话归属属于业务层（见 app/session.py）。
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 错误契约
#
# 上层不应该看见 httpx 的异常类型，否则「换掉 LiveTalking」就会波及业务代码。
# 这里把失败归纳成两种，路由层据此映射成合理的 HTTP 状态码。
# --------------------------------------------------------------------------


class DigitalHumanError(Exception):
    """数字人服务调用失败的基类。"""


class DigitalHumanUnavailable(DigitalHumanError):
    """连不上 LiveTalking：进程没启动、地址不对、网络不通或超时。"""


class DigitalHumanBadResponse(DigitalHumanError):
    """LiveTalking 的响应不可用：非 2xx，或 2xx 但缺少必需字段。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        # None 表示连 HTTP 状态都没拿到（例如应答不是合法 JSON）
        self.status_code = status_code


@dataclass(frozen=True)
class WebRTCSession:
    """一次 WebRTC 协商的结果。

    sdp / type 用于浏览器 setRemoteDescription；session_id 供后续 say / interrupt 使用。

    session_id 的类型按 LiveTalking 原样透传、不做转换——它到底是数字还是字符串，
    尚未在真实服务上验证。
    """

    sdp: str
    type: str
    session_id: str | int


class DigitalHumanService(ABC):
    """数字人服务接口。业务层只依赖它。"""

    @abstractmethod
    async def negotiate_webrtc(self, sdp: str, sdp_type: str) -> WebRTCSession:
        """把浏览器的 SDP offer 转交给数字人后端，换回 SDP answer 与会话标识。

        远端会话就是在这一步真正建立的——LiveTalking 没有「创建空会话」的接口。
        """

    @abstractmethod
    async def say(self, session_id: str | int, text: str, interrupt: bool = False) -> None:
        """让指定会话的数字人说出 text（由后端完成 TTS 与口型同步）。"""

    @abstractmethod
    async def interrupt(self, session_id: str | int) -> None:
        """打断指定会话正在进行的播报。"""

    @abstractmethod
    async def list_sessions(self) -> list[dict[str, Any]]:
        """列出后端当前的活跃会话。

        会话不总是由本服务创建的——例如浏览器经 MediaMTX 拉流时，会话是
        MediaMTX 调 /whep 建立的，本地无从得知它的 sessionid，只能反查。
        """

    @abstractmethod
    async def is_speaking(self, session_id: str | int) -> bool | None:
        """查询指定会话是否正在说话。

        返回 None 表示该会话在后端不存在（区别于「存在但没在说话」的 False）。
        """


class LiveTalkingService(DigitalHumanService):
    """通过 HTTP 调用远端 LiveTalking 的实现。

    与 LiveTalking API 的对应关系：

        negotiate_webrtc()  ->  POST /offer
        say()               ->  POST /human
        interrupt()         ->  POST /interrupt_talk
    """

    # 单次 HTTP 请求超时（秒）。TTS 与口型同步在远端是异步进行的，
    # 这个超时只覆盖「请求被受理」，不覆盖「说完一整段话」。
    _TIMEOUT = 10.0

    def __init__(
        self,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # base_url 由配置注入（AVATAR_BASE_URL），指向本地还是云端只是取值不同，
        # 本类内部没有任何地址硬编码。
        self._base_url = base_url.rstrip("/")
        # transport 仅供测试注入 httpx.MockTransport，生产路径保持为 None。
        self._transport = transport

    async def negotiate_webrtc(self, sdp: str, sdp_type: str) -> WebRTCSession:
        payload = await self._request("POST", "/offer", {"sdp": sdp, "type": sdp_type})

        # 真实应答形如 {"sdp": "...", "type": "answer", "sessionid": 1}
        missing = [key for key in ("sdp", "sessionid") if key not in payload]
        if missing:
            # 显式报错，而不是把 None 一路传到浏览器，让问题在更远的地方才炸
            raise DigitalHumanBadResponse(
                f"POST /offer 的应答缺少字段: {', '.join(missing)}"
            )

        return WebRTCSession(
            sdp=payload["sdp"],
            type=payload.get("type", "answer"),
            session_id=payload["sessionid"],
        )

    async def say(self, session_id: str | int, text: str, interrupt: bool = False) -> None:
        """把 text 交给 LiveTalking 朗读。

        type 固定为 "echo"，不可改成 "chat"：
        二者在 LiveTalking 里的区别是——echo 表示「原样朗读传入的文本」，
        chat 表示「让 LiveTalking 在云端自己调 LLM 生成回答」。
        本项目的 LLM（Qwen）必须跑在本地，云端只负责发声，
        所以绝不能把生成权交给远端。这是架构约束，不是接口偏好。
        """
        await self._request(
            "POST",
            "/human",
            {
                "sessionid": session_id,
                "text": text,
                "type": "echo",
                "interrupt": interrupt,
            },
        )

    async def interrupt(self, session_id: str | int) -> None:
        """请求 LiveTalking 清空音频队列，打断正在进行的播报。"""
        await self._request("POST", "/interrupt_talk", {"sessionid": session_id})

    async def list_sessions(self) -> list[dict[str, Any]]:
        """查询 LiveTalking 的活跃会话。

        返回顺序即会话的创建顺序（LiveTalking 用 dict 保存会话，Python 3.7+
        保序），因此最后一个就是最近创建的。
        """
        payload = await self._request("GET", "/api/admin/sessions")
        sessions = payload.get("data", {}).get("sessions")
        return sessions if isinstance(sessions, list) else []

    async def is_speaking(self, session_id: str | int) -> bool | None:
        """查询会话是否正在说话；会话不存在时返回 None。

        LiveTalking 用 {code, msg, data} 表达业务结果，HTTP 状态一律 200，
        所以这里必须看 code 而不是状态码——否则「会话不存在」会被当成
        「没在说话」，把错误静默掉。
        """
        payload = await self._request("POST", "/is_speaking", {"sessionid": session_id})
        if payload.get("code") != 0:
            logger.warning(
                "查询会话状态失败：sessionid=%s -> %s", session_id, payload.get("msg")
            )
            return None
        return bool(payload.get("data"))

    def _build_client(self) -> httpx.AsyncClient:
        """构造 HTTP 客户端。

        trust_env=False 是必需的，不是优化：httpx 默认 trust_env=True，会去读
        系统代理设置（Windows 上即注册表里的「Internet 选项」，Clash、企业 VPN
        之类工具都会写）。而 ProxyOverride 里的 127.* / localhost 白名单 httpx
        并不识别。

        后果是：连不上 LiveTalking 时，请求先被送进代理，拿回一个由代理产生的
        502，于是错误被误报成「LiveTalking 返回了错误」，而真相是「连不上」。
        这里直连目标地址；真需要走代理时应显式配置，而不是依赖环境。
        """
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._TIMEOUT,
            transport=self._transport,
            trust_env=False,
        )

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """向 LiveTalking 发一次请求，并把失败归纳成上层的错误类型。

        注意：这里刻意不记录 payload —— SDP 与文本没必要出现在日志里。
        出错时只记录方法与路径，足够定位问题。
        """
        async with self._build_client() as client:
            try:
                response = await client.request(method, path, json=payload)
            except httpx.RequestError as exc:
                logger.warning(
                    "LiveTalking 不可达：%s %s（%s）", method, path, exc.__class__.__name__
                )
                raise DigitalHumanUnavailable(
                    f"无法连接 LiveTalking: {method} {path}"
                ) from exc

        if response.status_code >= 400:
            logger.warning(
                "LiveTalking 返回错误：%s %s -> HTTP %s",
                method,
                path,
                response.status_code,
            )
            raise DigitalHumanBadResponse(
                f"{method} {path} -> HTTP {response.status_code}", response.status_code
            )

        try:
            return response.json()
        except ValueError as exc:
            logger.warning("LiveTalking 应答不是合法 JSON：POST %s", path)
            raise DigitalHumanBadResponse(f"POST {path} 的应答不是合法 JSON") from exc
