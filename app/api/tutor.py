"""教育教练模式 API。

Tutor 与普通聊天共享数字人 session、interrupt epoch 和 ConversationStore 锁，
但教学业务状态保存在独立的 TutorSessionStore 中。
"""

import logging
import time
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException

from app.api import chat as chat_api
from app.schemas import (
    ChatMetrics,
    TutorAnswerRequest,
    TutorAnswerResponse,
    TutorResetResponse,
    TutorStartRequest,
    TutorStartResponse,
)
from app.services import get_llm_service, get_service
from app.services.digital_human import DigitalHumanError, DigitalHumanService
from app.services.llm import LLMBadResponse, LLMService
from app.tutor import (
    TutorAnswerParseError,
    TutorState,
    build_tutor_answer_instruction,
    build_tutor_start_instruction,
    parse_tutor_answer,
    tutor_session_store,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tutor", tags=["tutor"])


@dataclass(frozen=True)
class _DeliveryResult:
    status: str
    verified_speaking: bool
    avatar_startup_ms: int | None
    avatar_startup_status: str


async def _deliver_and_observe(
    service: DigitalHumanService,
    session_id: str,
    text: str,
    answer_ready_at: float,
) -> _DeliveryResult:
    """发送已生成文本并尽力观测起播；指标失败不改变受理语义。"""
    speaking_baseline = await chat_api._observe_speaking_baseline(service, session_id)
    await service.say(session_id=session_id, text=text, interrupt=False)

    confirmation_unavailable = False
    try:
        verified = await chat_api._confirm_speaking(service, session_id)
    except DigitalHumanError as exc:
        logger.warning("教学起播确认失败，/human 已受理，继续提交业务状态：%s", exc)
        verified = False
        confirmation_unavailable = True

    avatar_startup_ms: int | None = None
    if speaking_baseline is True:
        avatar_startup_status = "already_speaking"
    elif speaking_baseline is None or confirmation_unavailable:
        avatar_startup_status = "unavailable"
    elif verified:
        avatar_startup_ms = chat_api._elapsed_ms(answer_ready_at, time.perf_counter())
        avatar_startup_status = "measured"
    else:
        avatar_startup_status = "not_observed"

    return _DeliveryResult(
        status="accepted" if verified else "accepted_unverified",
        verified_speaking=verified,
        avatar_startup_ms=avatar_startup_ms,
        avatar_startup_status=avatar_startup_status,
    )


def _metrics(llm_ms: int, delivery: _DeliveryResult) -> ChatMetrics:
    return ChatMetrics(
        llm_ms=llm_ms,
        avatar_startup_ms=delivery.avatar_startup_ms,
        avatar_startup_status=delivery.avatar_startup_status,
    )


@router.post("/start", response_model=TutorStartResponse)
async def tutor_start(
    payload: TutorStartRequest,
    service: DigitalHumanService = Depends(get_service),
    llm: LLMService = Depends(get_llm_service),
) -> TutorStartResponse:
    """清理旧上下文并生成、播报当前主题的第一道题。"""
    session_id = await chat_api._resolve_session_id(service)
    lock = chat_api.conversation_store.lock_for(session_id)

    async with lock:
        # 开始新教学即放弃当前 session 的旧教学状态与聊天上下文。
        tutor_session_store.clear(session_id)
        chat_api.conversation_store.clear(session_id)

        start_user = f"开始学习主题：{payload.topic}"
        instruction = build_tutor_start_instruction(payload.topic)
        epoch = chat_api._current_interrupt_epoch()

        llm_started_at = time.perf_counter()
        raw_question = await llm.chat(
            [{"role": "user", "content": start_user}],
            instruction=instruction,
        )
        answer_ready_at = time.perf_counter()
        llm_ms = chat_api._elapsed_ms(llm_started_at, answer_ready_at)
        question = raw_question.strip()
        if not question:
            raise LLMBadResponse("模型返回了空的教学问题")

        if chat_api._current_interrupt_epoch() != epoch:
            return TutorStartResponse(
                status="cancelled",
                session_id=session_id,
                topic=payload.topic,
                question=question,
                question_index=1,
                verified_speaking=False,
                metrics=ChatMetrics(
                    llm_ms=llm_ms,
                    avatar_startup_ms=None,
                    avatar_startup_status="cancelled",
                ),
            )

        delivery = await _deliver_and_observe(
            service,
            session_id,
            question,
            answer_ready_at,
        )

        # /human 已成功受理后才同时提交教学状态与完整对话 turn。
        tutor_session_store.set(
            session_id,
            TutorState(
                mode=payload.mode,
                topic=payload.topic,
                current_question=question,
                question_index=1,
            ),
        )
        chat_api.conversation_store.append_turn(session_id, start_user, question)

        return TutorStartResponse(
            status=delivery.status,
            session_id=session_id,
            topic=payload.topic,
            question=question,
            question_index=1,
            verified_speaking=delivery.verified_speaking,
            metrics=_metrics(llm_ms, delivery),
        )


@router.post("/answer", response_model=TutorAnswerResponse)
async def tutor_answer(
    payload: TutorAnswerRequest,
    service: DigitalHumanService = Depends(get_service),
    llm: LLMService = Depends(get_llm_service),
) -> TutorAnswerResponse:
    """评价当前回答、给出解释，并推进到下一道题。"""
    session_id = await chat_api._resolve_session_id(service)
    lock = chat_api.conversation_store.lock_for(session_id)

    async with lock:
        state = tutor_session_store.get(session_id)
        if state is None:
            raise HTTPException(status_code=409, detail="No active tutor session")

        messages = chat_api.conversation_store.get_messages(session_id)
        messages.append({"role": "user", "content": payload.text})
        instruction = build_tutor_answer_instruction(state)
        epoch = chat_api._current_interrupt_epoch()

        llm_started_at = time.perf_counter()
        raw_answer = await llm.chat(messages, instruction=instruction)
        answer_ready_at = time.perf_counter()
        llm_ms = chat_api._elapsed_ms(llm_started_at, answer_ready_at)

        try:
            result = parse_tutor_answer(raw_answer)
        except TutorAnswerParseError as exc:
            raise LLMBadResponse("模型返回的教学回答格式无效") from exc

        next_index = state.question_index + 1
        if chat_api._current_interrupt_epoch() != epoch:
            return TutorAnswerResponse(
                status="cancelled",
                session_id=session_id,
                evaluation=result.evaluation,
                explanation=result.explanation,
                next_question=result.next_question,
                question_index=next_index,
                verified_speaking=False,
                metrics=ChatMetrics(
                    llm_ms=llm_ms,
                    avatar_startup_ms=None,
                    avatar_startup_status="cancelled",
                ),
            )

        delivery = await _deliver_and_observe(
            service,
            session_id,
            raw_answer.strip(),
            answer_ready_at,
        )

        # 解析、打断检查和 /human 受理均成功后才提交完整 turn 并推进题号。
        chat_api.conversation_store.append_turn(session_id, payload.text, raw_answer.strip())
        tutor_session_store.set(
            session_id,
            TutorState(
                mode=state.mode,
                topic=state.topic,
                current_question=result.next_question,
                question_index=next_index,
            ),
        )

        return TutorAnswerResponse(
            status=delivery.status,
            session_id=session_id,
            evaluation=result.evaluation,
            explanation=result.explanation,
            next_question=result.next_question,
            question_index=next_index,
            verified_speaking=delivery.verified_speaking,
            metrics=_metrics(llm_ms, delivery),
        )


@router.post("/reset", response_model=TutorResetResponse)
async def tutor_reset(
    service: DigitalHumanService = Depends(get_service),
) -> TutorResetResponse:
    """清空当前 session 的教学状态和教学对话，不改变数字人 session。"""
    session_id = await chat_api._resolve_session_id(service)
    lock = chat_api.conversation_store.lock_for(session_id)

    async with lock:
        tutor_session_store.clear(session_id)
        chat_api.conversation_store.clear(session_id)

    return TutorResetResponse(session_id=session_id)
