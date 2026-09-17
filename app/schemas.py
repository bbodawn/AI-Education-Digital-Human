"""HTTP 接口的请求与响应模型。

只定义当前真正被路由使用的模型，不为将来预留。
"""

from pydantic import BaseModel, Field


class WebRTCOfferRequest(BaseModel):
    """浏览器发来的 SDP offer，原样转交给 LiveTalking。"""

    sdp: str = Field(min_length=1)
    type: str = "offer"


class WebRTCOfferResponse(BaseModel):
    """协商结果。session_id 供后续 say / interrupt 使用。"""

    sdp: str
    type: str
    session_id: str | int


class ChatSayRequest(BaseModel):
    """让数字人朗读一段文本。"""

    text: str = Field(min_length=1)
    interrupt: bool = False


class ChatSayResponse(BaseModel):
    """表示请求已被数字人服务受理，不代表播报已经结束。

    session_id 一并返回，是为了让「文本到底发给哪个会话了」这件事可诊断——
    会话由 MediaMTX 拉流时创建，本地只能反查，选错时必须看得见。
    """

    # accepted = 已受理且观察到开始播报；accepted_unverified = 已受理但未观察到播报
    status: str
    session_id: str
    verified_speaking: bool


class ChatInterruptResponse(BaseModel):
    status: str = "interrupted"


class ChatResetResponse(BaseModel):
    """当前数字人会话的短期对话上下文已清空。"""

    status: str = "reset"
    session_id: str


class ChatAskRequest(BaseModel):
    """向数字人提一个问题，由本地大模型作答。"""

    text: str = Field(min_length=1)


class ChatMetrics(BaseModel):
    """一次问答中可观测到的服务端阶段耗时。"""

    llm_ms: int = Field(ge=0)
    avatar_startup_ms: int | None = Field(default=None, ge=0)
    avatar_startup_status: str


class ChatAskResponse(BaseModel):
    """一次「提问 → 模型生成 → 数字人朗读」的完整结果。

    answer 一并返回，是为了让用户既能看到「AI 想说什么」，也能对照
    「数字人正在说什么」——两者不一致时，一眼就能发现。
    """

    # accepted = 已受理且观察到播报
    # accepted_unverified = 已受理但未观察到播报
    # cancelled = 生成期间用户点了打断，因此没有播报
    status: str
    question: str
    answer: str
    session_id: str
    verified_speaking: bool
    metrics: ChatMetrics


class ASRTranscriptionResponse(BaseModel):
    """音频文件的最小转写结果。"""

    text: str
