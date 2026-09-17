"""对话控制。

浏览器不自己管理 session_id——它只发文本，由本地 FastAPI 决定发往哪个会话。

两条发送路径：
  /say  把给定文本直接交给数字人念（V0.1 起就有）
  /ask  先让本地大模型生成回答，再交给数字人念（V0.2 新增）

/ask 是「新增」而不是改写 /say，因为两者的语义不同：一个是「念这段话」，
一个是「回答这个问题」。混在一起会让调用方无法表达自己的意图。
"""

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException

from app.conversation import conversation_store
from app.schemas import (
    ChatAskRequest,
    ChatAskResponse,
    ChatMetrics,
    ChatInterruptResponse,
    ChatResetResponse,
    ChatSayRequest,
    ChatSayResponse,
)
from app.services import get_llm_service, get_service
from app.services.digital_human import DigitalHumanError, DigitalHumanService
from app.services.llm import LLMService
from app.session import get_session_id, set_session_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

# 播报不是立刻开始的：文本先进队列，TTS 合成出音频后才进入 speaking 状态。
# 因此发送后要轮询一小段时间来确认，而不是查一次就下结论。
_SPEAKING_POLL_ATTEMPTS = 10
_SPEAKING_POLL_INTERVAL = 0.5  # 秒，合计最多等 5 秒

# 打断计数器。模型生成要几秒，这期间用户完全可能已经不想听了——
# 用一个单调递增的计数就能表达「我打断过」，不需要引入任务队列。
# V0.2 是单用户场景，进程内一个整数够用。
_interrupt_epoch = 0


def _bump_interrupt_epoch() -> None:
    global _interrupt_epoch
    _interrupt_epoch += 1


def _current_interrupt_epoch() -> int:
    return _interrupt_epoch


async def _resolve_session_id(service: DigitalHumanService) -> str:
    """确定文本要发往哪个会话。

    优先用本地缓存的 session_id。没有缓存时反查后端——因为浏览器经 MediaMTX
    拉流时，会话是 MediaMTX 调 /whep 建立的，本地从来没参与过。

    反查取「最后创建的那个」：后端按创建顺序返回会话（dict 保序），而
    MediaMTX 的会话总是最后建立的。这个选择不保证正确，所以会打进日志，
    并且响应里会回带实际使用的 session_id，出问题时看得见。
    """
    cached = get_session_id()
    if cached is not None:
        return str(cached)

    sessions = await service.list_sessions()
    if not sessions:
        raise HTTPException(
            status_code=409,
            detail="No active avatar session on LiveTalking",
        )

    candidates = [str(s.get("sessionid", "")) for s in sessions]
    chosen = candidates[-1]
    logger.warning(
        "本地无会话缓存，从 LiveTalking 反查到 %d 个会话：%s，选用最后创建的 %s",
        len(candidates),
        candidates,
        chosen,
    )
    set_session_id(chosen)
    return chosen


async def _confirm_speaking(service: DigitalHumanService, session_id: str) -> bool:
    """轮询确认会话真的进入了播报状态。

    注意：这只能证明「后端确实在处理这段文本」，不能证明「发对了会话」——
    LiveTalking 里每个会话都有独立的渲染循环，发错会话同样会进入 speaking。
    真正对不对，最终要靠浏览器里的画面来判断。
    """
    for _ in range(_SPEAKING_POLL_ATTEMPTS):
        state = await service.is_speaking(session_id)
        if state is None:
            logger.warning("会话已不存在：%s", session_id)
            return False
        if state:
            return True
        await asyncio.sleep(_SPEAKING_POLL_INTERVAL)
    return False


async def _observe_speaking_baseline(
    service: DigitalHumanService, session_id: str
) -> bool | None:
    """尽力取得发送本轮回答前的播报状态，仅用于指标判定。

    这是观测能力，不是业务前置条件。查询失败时返回 None，后续 /human 与原有
    speaking 确认仍照常执行，避免指标故障改变问答成功语义。
    """
    try:
        return await service.is_speaking(session_id)
    except DigitalHumanError as exc:
        logger.warning("起播指标基线查询失败，继续正常播报：%s", exc)
        return None


def _elapsed_ms(started_at: float, finished_at: float) -> int:
    """把单调时钟差转换为非负整数毫秒。"""
    return max(0, round((finished_at - started_at) * 1000))


@router.post("/say", response_model=ChatSayResponse)
async def chat_say(
    payload: ChatSayRequest,
    service: DigitalHumanService = Depends(get_service),
) -> ChatSayResponse:
    """让数字人朗读一段给定的文本（不经模型）。

    返回 200 只代表请求已被数字人服务受理，不代表它已经说完了。
    """
    session_id = await _resolve_session_id(service)

    await service.say(
        session_id=session_id,
        text=payload.text,
        interrupt=payload.interrupt,
    )

    verified = await _confirm_speaking(service, session_id)
    if not verified:
        logger.warning(
            "文本已受理但未观察到播报：sessionid=%s。若画面无反应，"
            "说明选中的会话可能不是浏览器当前观看的那个。",
            session_id,
        )

    return ChatSayResponse(
        status="accepted" if verified else "accepted_unverified",
        session_id=session_id,
        verified_speaking=verified,
    )


@router.post("/ask", response_model=ChatAskResponse)
async def chat_ask(
    payload: ChatAskRequest,
    service: DigitalHumanService = Depends(get_service),
    llm: LLMService = Depends(get_llm_service),
) -> ChatAskResponse:
    """问数字人一个问题：本地大模型生成回答，然后由数字人念出来。

    先解析会话再调模型——没有可播报的会话时就不该浪费一次生成。
    """
    session_id = await _resolve_session_id(service)
    lock = conversation_store.lock_for(session_id)

    async with lock:
        messages = conversation_store.get_messages(session_id)
        messages.append({"role": "user", "content": payload.text})

        # 记下开始生成时的打断计数，生成结束后比对
        epoch = _current_interrupt_epoch()

        # 锁等待和历史读取不计入 llm_ms；该指标仍只覆盖实际模型调用。
        llm_started_at = time.perf_counter()
        answer = await llm.chat(messages)
        answer_ready_at = time.perf_counter()
        llm_ms = _elapsed_ms(llm_started_at, answer_ready_at)
        logger.info("模型回答生成完成（%d 字），sessionid=%s", len(answer), session_id)

        if _current_interrupt_epoch() != epoch:
            # 生成期间用户点了打断。这句话已经过时了，不能播报或写入历史。
            logger.info("生成期间收到打断，跳过播报：sessionid=%s", session_id)
            return ChatAskResponse(
                status="cancelled",
                question=payload.text,
                answer=answer,
                session_id=session_id,
                verified_speaking=False,
                metrics=ChatMetrics(
                    llm_ms=llm_ms,
                    avatar_startup_ms=None,
                    avatar_startup_status="cancelled",
                ),
            )

        speaking_baseline = await _observe_speaking_baseline(service, session_id)
        await service.say(session_id=session_id, text=answer, interrupt=False)

        verified = await _confirm_speaking(service, session_id)
        if not verified:
            logger.warning(
                "回答已受理但未观察到播报：sessionid=%s。若画面无反应，"
                "说明选中的会话可能不是浏览器当前观看的那个。",
                session_id,
            )

        avatar_startup_ms: int | None = None
        if speaking_baseline is True:
            avatar_startup_status = "already_speaking"
        elif speaking_baseline is None:
            avatar_startup_status = "unavailable"
        elif verified:
            avatar_startup_ms = _elapsed_ms(answer_ready_at, time.perf_counter())
            avatar_startup_status = "measured"
        else:
            avatar_startup_status = "not_observed"

        # 只有正常业务响应才提交完整 turn；异常和 cancelled 都不会走到这里。
        conversation_store.append_turn(session_id, payload.text, answer)

        return ChatAskResponse(
            status="accepted" if verified else "accepted_unverified",
            question=payload.text,
            answer=answer,
            session_id=session_id,
            verified_speaking=verified,
            metrics=ChatMetrics(
                llm_ms=llm_ms,
                avatar_startup_ms=avatar_startup_ms,
                avatar_startup_status=avatar_startup_status,
            ),
        )


@router.post("/reset", response_model=ChatResetResponse)
async def chat_reset(
    service: DigitalHumanService = Depends(get_service),
) -> ChatResetResponse:
    """清空当前会话的短期对话历史，不改变数字人或播报状态。"""
    session_id = await _resolve_session_id(service)
    lock = conversation_store.lock_for(session_id)

    async with lock:
        conversation_store.clear(session_id)

    return ChatResetResponse(session_id=session_id)


@router.post("/interrupt", response_model=ChatInterruptResponse)
async def chat_interrupt(
    service: DigitalHumanService = Depends(get_service),
) -> ChatInterruptResponse:
    """打断当前播报。

    同时递增打断计数：这样即便此刻模型还在生成回答，它返回后也会发现自己
    已经过时，从而不会接着播报。
    """
    _bump_interrupt_epoch()
    session_id = await _resolve_session_id(service)
    await service.interrupt(session_id=session_id)
    return ChatInterruptResponse()
