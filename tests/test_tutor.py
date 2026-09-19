"""教育教练状态、Prompt 和输出解析的纯逻辑测试。"""

import pytest

from app.tutor import (
    TUTOR_MODE_GUIDED_QA,
    TutorAnswerParseError,
    TutorAnswerResult,
    TutorSessionStore,
    TutorState,
    build_tutor_answer_instruction,
    build_tutor_start_instruction,
    parse_tutor_answer,
)


def make_state(
    *,
    mode: str = TUTOR_MODE_GUIDED_QA,
    topic: str = "RAG 基础",
    question: str = "RAG 为什么需要检索器？",
    index: int = 2,
) -> TutorState:
    return TutorState(
        mode=mode,
        topic=topic,
        current_question=question,
        question_index=index,
    )


def test_tutor_state_has_minimal_guided_qa_fields() -> None:
    state = make_state()

    assert state.mode == "guided_qa"
    assert state.topic == "RAG 基础"
    assert state.current_question == "RAG 为什么需要检索器？"
    assert state.question_index == 2


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"mode": "unsupported"}, "unsupported tutor mode"),
        ({"topic": "   "}, "topic must not be empty"),
        ({"question": ""}, "current_question must not be empty"),
        ({"index": 0}, "question_index must start at 1"),
    ],
)
def test_tutor_state_rejects_invalid_values(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        make_state(**kwargs)


def test_tutor_store_set_get_and_normalize_session_id() -> None:
    store = TutorSessionStore()
    state = make_state()

    store.set(1, state)

    assert store.get("1") is state


def test_tutor_store_isolates_sessions_and_clear_is_scoped() -> None:
    store = TutorSessionStore()
    state_a = make_state(topic="主题 A")
    state_b = make_state(topic="主题 B")
    store.set("A", state_a)
    store.set("B", state_b)

    store.clear("A")

    assert store.get("A") is None
    assert store.get("B") is state_b
    store.clear("missing")


def test_start_instruction_contains_topic_first_question_and_voice_constraints() -> None:
    instruction = build_tutor_start_instruction("RAG 基础")

    assert "教育教练" in instruction
    assert "学习主题：RAG 基础" in instruction
    assert "当前题号：1" in instruction
    assert "第一道问题" in instruction
    assert "一次只能提出一道题" in instruction
    assert "口语播报" in instruction


def test_answer_instruction_contains_current_tutor_state_and_output_contract() -> None:
    state = make_state()

    instruction = build_tutor_answer_instruction(state)

    assert "学习主题：RAG 基础" in instruction
    assert "当前问题：RAG 为什么需要检索器？" in instruction
    assert "当前题号：2" in instruction
    assert "评价：..." in instruction
    assert "解释：..." in instruction
    assert "下一题：..." in instruction


def test_parse_tutor_answer_extracts_all_sections() -> None:
    result = parse_tutor_answer(
        "评价：回答基本正确。\n"
        "解释：检索器负责从知识库找到相关内容。\n"
        "它为生成阶段提供依据。\n"
        "下一题：生成器如何利用检索结果？"
    )

    assert result == TutorAnswerResult(
        evaluation="回答基本正确。",
        explanation="检索器负责从知识库找到相关内容。\n它为生成阶段提供依据。",
        next_question="生成器如何利用检索结果？",
    )


def test_parse_tutor_answer_fails_when_next_question_is_missing() -> None:
    with pytest.raises(TutorAnswerParseError, match="下一题"):
        parse_tutor_answer("评价：正确。\n解释：解释内容。")


def test_parse_tutor_answer_fails_when_next_question_is_empty() -> None:
    with pytest.raises(TutorAnswerParseError, match="下一题"):
        parse_tutor_answer("评价：正确。\n解释：解释内容。\n下一题：   ")
