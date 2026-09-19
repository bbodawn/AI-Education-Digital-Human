r"""使用真实 Ollama 手工验收 Tutor Prompt；不经过 FastAPI 或数字人链路。

运行：
    .\.venv\Scripts\python.exe scripts\manual_tutor_prompt_check.py

脚本故意不做格式修复或自动重试。解析失败会原样记录并终止当前教学链路，
用于测量现有生产 Prompt 的真实服从率。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings  # noqa: E402
from app.conversation import ConversationStore  # noqa: E402
from app.services.llm import LLMError, OllamaService  # noqa: E402
from app.tutor import (  # noqa: E402
    TUTOR_MODE_GUIDED_QA,
    TutorAnswerParseError,
    TutorState,
    build_tutor_answer_instruction,
    build_tutor_start_instruction,
    parse_tutor_answer,
)


TOPIC = "RAG 基础"
SESSION_ID = "manual-tutor-prompt-check"
EXPECTED_OLLAMA_URL = "http://127.0.0.1:11434"
EXPECTED_MODEL = "qwen2.5:7b"
ANSWER_TYPES = (
    "正常且较完整回答",
    "部分正确回答",
    "明显错误回答",
    "非常简短回答",
    "不知道",
    "正常回答（history 已开始裁剪）",
    "简短回答（history 已裁剪）",
    "明显错误回答（history 已裁剪）",
)
KNOWN_FLAGS = (
    "topic_drift",
    "duplicate",
    "multi_question",
    "mismatch",
    "too_long",
    "chat",
)


def configure_console() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def answer_type_for(turn_index: int) -> str:
    return ANSWER_TYPES[(turn_index - 1) % len(ANSWER_TYPES)]


def ask_observation_flags() -> set[str]:
    print("观察标记（可多选，逗号分隔；无异常直接回车）：")
    print("  " + ", ".join(KNOWN_FLAGS))
    raw = input("Flags: ").strip().lower()
    if not raw:
        return set()
    return {item.strip() for item in raw.replace("，", ",").split(",") if item.strip()}


def missing_protocol_labels(raw: str) -> list[str]:
    return [label for label in ("评价：", "解释：", "下一题：") if label not in raw]


def normalized_question(question: str) -> str:
    return "".join(question.lower().split())


async def run_acceptance(turns: int) -> int:
    print("=== Environment ===")
    print(f"Ollama: {settings.ollama_base_url}")
    print(f"Model: {settings.ollama_model}")
    print(f"Turns requested: {turns}")

    if settings.ollama_base_url.rstrip("/") != EXPECTED_OLLAMA_URL:
        print(f"FAIL: OLLAMA_BASE_URL 必须为 {EXPECTED_OLLAMA_URL}")
        return 2
    if settings.ollama_model != EXPECTED_MODEL:
        print(f"FAIL: OLLAMA_MODEL 必须为 {EXPECTED_MODEL}")
        return 2

    llm = OllamaService(settings.ollama_base_url, settings.ollama_model)
    history = ConversationStore(max_history_turns=5)
    answer_count = 0
    parse_success_count = 0
    parse_failure_count = 0
    missing_labels: Counter[str] = Counter()
    observation_counts: Counter[str] = Counter()
    seen_questions: set[str] = set()
    completed_answer_types: list[str] = []

    start_user = f"开始学习主题：{TOPIC}"
    print("\n=== Tutor Start ===")
    print(f"Topic: {TOPIC}")
    started_at = time.perf_counter()
    try:
        current_question = await llm.chat(
            [{"role": "user", "content": start_user}],
            instruction=build_tutor_start_instruction(TOPIC),
        )
    except LLMError as exc:
        print(f"Tutor Start: FAIL ({exc})")
        return 1

    current_question = current_question.strip()
    if not current_question:
        print("Tutor Start: FAIL（模型返回空问题）")
        return 1

    print(f"Latency: {time.perf_counter() - started_at:.3f} s")
    print("Raw question:")
    print(current_question)
    start_flags = ask_observation_flags()
    observation_counts.update(start_flags)
    history.append_turn(SESSION_ID, start_user, current_question)
    seen_questions.add(normalized_question(current_question))

    current_index = 1
    for turn_number in range(1, turns + 1):
        requested_type = answer_type_for(turn_number)
        state = TutorState(
            mode=TUTOR_MODE_GUIDED_QA,
            topic=TOPIC,
            current_question=current_question,
            question_index=current_index,
        )
        history_messages = history.get_messages(SESSION_ID)

        print(f"\n=== Turn {turn_number} ===")
        print(f"Question index: {current_index}")
        print(f"History turns sent: {len(history_messages) // 2} / 5")
        print(f"Requested answer type: {requested_type}")
        print("Question:")
        print(current_question)
        user_answer = input("Your answer: ").strip()
        if not user_answer:
            print("输入为空，本次人工验收停止；不会代替用户生成答案。")
            break

        messages = history_messages
        messages.append({"role": "user", "content": user_answer})
        answer_count += 1
        completed_answer_types.append(requested_type)
        started_at = time.perf_counter()
        try:
            raw = await llm.chat(
                messages,
                instruction=build_tutor_answer_instruction(state),
            )
        except LLMError as exc:
            parse_failure_count += 1
            print(f"LLM call: FAIL ({exc})")
            break


        print(f"Latency: {time.perf_counter() - started_at:.3f} s")
        print("Raw:")
        print(raw)
        for label in missing_protocol_labels(raw):
            missing_labels[label] += 1

        try:
            result = parse_tutor_answer(raw)
        except TutorAnswerParseError as exc:
            parse_failure_count += 1
            print(f"Parse: FAIL ({exc})")
            flags = ask_observation_flags()
            observation_counts.update(flags)
            print("解析失败：不重试、不修复，也不伪造 next_question；教学链路停止。")
            break

        parse_success_count += 1
        print("Parse: PASS")
        print(f"Evaluation: {result.evaluation}")
        print(f"Explanation: {result.explanation}")
        print(f"Next question: {result.next_question}")

        next_key = normalized_question(result.next_question)
        automatic_flags: set[str] = set()
        if next_key in seen_questions:
            automatic_flags.add("duplicate")
        if result.next_question.count("？") + result.next_question.count("?") > 1:
            automatic_flags.add("multi_question")
        if automatic_flags:
            print("自动观察标记：" + ", ".join(sorted(automatic_flags)))

        flags = automatic_flags | ask_observation_flags()
        observation_counts.update(flags)
        history.append_turn(SESSION_ID, user_answer, raw.strip())
        seen_questions.add(next_key)
        current_question = result.next_question
        current_index += 1

    compliance = (parse_success_count / answer_count * 100) if answer_count else 0.0
    print("\n=== Summary ===")
    print(f"Answers: {answer_count}")
    print(f"Parse successes: {parse_success_count}")
    print(f"Parse failures: {parse_failure_count}")
    print(f"Format compliance: {compliance:.2f}%")
    print(f"Missing 评价： {missing_labels['评价：']}")
    print(f"Missing 解释： {missing_labels['解释：']}")
    print(f"Missing 下一题： {missing_labels['下一题：']}")
    print(f"Topic drift: {observation_counts['topic_drift']}")
    print(f"Duplicate questions: {observation_counts['duplicate']}")
    print(f"Multi-question violations: {observation_counts['multi_question']}")
    print(f"Evaluation mismatches: {observation_counts['mismatch']}")
    print(f"Overlong explanations: {observation_counts['too_long']}")
    print(f"Ordinary-chat drift: {observation_counts['chat']}")
    print("Completed answer types:")
    for index, answer_type in enumerate(completed_answer_types, start=1):
        print(f"  {index}. {answer_type}")

    return 0 if answer_count >= 7 and parse_failure_count == 0 else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="真实 Qwen Tutor Prompt 人工验收")
    parser.add_argument(
        "--turns",
        type=int,
        choices=range(7, 11),
        default=8,
        metavar="7-10",
        help="人工回答轮数，默认 8",
    )
    return parser.parse_args()


def main() -> int:
    configure_console()
    args = parse_args()
    try:
        return asyncio.run(run_acceptance(args.turns))
    except (EOFError, KeyboardInterrupt):
        print("\n人工验收已由用户中止。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
