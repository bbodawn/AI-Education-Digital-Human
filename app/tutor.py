"""教育教练模式的进程内状态与纯文本 Prompt 契约。

本模块不负责 API、并发事务或模型调用。TutorSessionStore 只保存按 session_id
隔离的教学状态；后续 Tutor API 必须复用 ConversationStore 的 session lock。
"""

from dataclasses import dataclass


TUTOR_MODE_GUIDED_QA = "guided_qa"


@dataclass(frozen=True)
class TutorState:
    """一场最小引导问答所需的教学状态。"""

    mode: str
    topic: str
    current_question: str
    question_index: int

    def __post_init__(self) -> None:
        if self.mode != TUTOR_MODE_GUIDED_QA:
            raise ValueError(f"unsupported tutor mode: {self.mode}")
        if not self.topic.strip():
            raise ValueError("topic must not be empty")
        if not self.current_question.strip():
            raise ValueError("current_question must not be empty")
        if self.question_index < 1:
            raise ValueError("question_index must start at 1")


class TutorSessionStore:
    """按 session_id 隔离的进程内教学状态，不自行管理事务锁。"""

    def __init__(self) -> None:
        self._states: dict[str, TutorState] = {}

    @staticmethod
    def _normalize_session_id(session_id: str | int) -> str:
        return str(session_id)

    def get(self, session_id: str | int) -> TutorState | None:
        return self._states.get(self._normalize_session_id(session_id))

    def set(self, session_id: str | int, state: TutorState) -> None:
        self._states[self._normalize_session_id(session_id)] = state

    def clear(self, session_id: str | int) -> None:
        self._states.pop(self._normalize_session_id(session_id), None)


class TutorAnswerParseError(ValueError):
    """模型回答不符合教学输出协议。"""


@dataclass(frozen=True)
class TutorAnswerResult:
    """从教学回答中提取出的三个明确部分。"""

    evaluation: str
    explanation: str
    next_question: str


def _require_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def build_tutor_start_instruction(topic: str) -> str:
    """构造开始教学时的附加 system instruction。"""
    normalized_topic = _require_text(topic, "topic")
    return (
        "你现在进入教育教练的引导问答模式。\n"
        f"学习主题：{normalized_topic}\n"
        "当前题号：1\n"
        "请主动提出第一道问题，帮助学习者理解该主题。\n"
        "一次只能提出一道题，不要同时给出多道题。\n"
        "措辞应简洁、清晰，并适合数字人口语播报。"
    )


def build_tutor_answer_instruction(state: TutorState) -> str:
    """构造评价当前回答并继续下一题的附加 system instruction。"""
    return (
        "你现在进入教育教练的引导问答模式。\n"
        f"学习主题：{state.topic}\n"
        f"当前题号：{state.question_index}\n"
        f"当前问题：{state.current_question}\n"
        "请评价学习者对当前问题的回答，给出简短解释，再提出下一道题。\n"
        "一次只能提出一道新题，内容应适合数字人口语播报。\n"
        "必须严格按以下三段输出，并保证“下一题”非空：\n"
        "评价：...\n"
        "解释：...\n"
        "下一题：..."
    )


def parse_tutor_answer(text: str) -> TutorAnswerResult:
    """解析评价/解释/下一题协议；缺失或空字段时明确失败。"""
    sections: dict[str, list[str]] = {}
    current_section: str | None = None
    labels = ("评价", "解释", "下一题")

    for raw_line in text.splitlines():
        line = raw_line.strip()
        matched_label = next(
            (label for label in labels if line.startswith(f"{label}：")),
            None,
        )
        if matched_label is not None:
            current_section = matched_label
            sections.setdefault(matched_label, []).append(
                line.removeprefix(f"{matched_label}：").strip()
            )
        elif current_section is not None:
            sections[current_section].append(line)

    def section(name: str) -> str:
        value = "\n".join(sections.get(name, ())).strip()
        if not value:
            raise TutorAnswerParseError(f"missing or empty tutor section: {name}")
        return value

    return TutorAnswerResult(
        evaluation=section("评价"),
        explanation=section("解释"),
        next_question=section("下一题"),
    )


# 进程内单例；应用重启后自然清空。事务锁由 ConversationStore 统一提供。
tutor_session_store = TutorSessionStore()
