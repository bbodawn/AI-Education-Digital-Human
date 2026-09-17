"""进程内短期对话上下文。

V0.4 Phase 1 只保留最近若干个完整问答轮次，不做持久化。历史提交单位固定为
user + assistant，避免模型失败后留下只有用户问题的半轮记录。
"""

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Literal, TypedDict


MAX_HISTORY_TURNS = 5


class ChatMessage(TypedDict):
    """传给 LLMService 的标准对话消息；system prompt 由 LLM 层统一管理。"""

    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class ConversationTurn:
    """一个不可拆分的完整问答轮次。"""

    user: str
    assistant: str


class ConversationStore:
    """按 session_id 隔离的进程内短期历史与并发锁。"""

    def __init__(self, max_history_turns: int = MAX_HISTORY_TURNS) -> None:
        if max_history_turns < 1:
            raise ValueError("max_history_turns must be at least 1")
        self._max_history_turns = max_history_turns
        self._histories: dict[str, deque[ConversationTurn]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _normalize_session_id(session_id: str | int) -> str:
        return str(session_id)

    def get_messages(self, session_id: str | int) -> list[ChatMessage]:
        """返回按 user/assistant 展开的历史副本，不包含 system prompt。"""
        history = self._histories.get(self._normalize_session_id(session_id), ())
        messages: list[ChatMessage] = []
        for turn in history:
            messages.append({"role": "user", "content": turn.user})
            messages.append({"role": "assistant", "content": turn.assistant})
        return messages

    def append_turn(
        self,
        session_id: str | int,
        user: str,
        assistant: str,
    ) -> None:
        """原子地追加一个完整轮次，超限时由 deque 淘汰最老轮次。"""
        key = self._normalize_session_id(session_id)
        history = self._histories.get(key)
        if history is None:
            history = deque(maxlen=self._max_history_turns)
            self._histories[key] = history
        history.append(ConversationTurn(user=user, assistant=assistant))

    def clear(self, session_id: str | int) -> None:
        """只清除指定 session 的历史；保留其 lock，避免产生并发锁竞态。"""
        self._histories.pop(self._normalize_session_id(session_id), None)

    def lock_for(self, session_id: str | int) -> asyncio.Lock:
        """为同一标准化 session_id 稳定返回同一个进程内锁。"""
        key = self._normalize_session_id(session_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock


# 进程内单例：FastAPI 重启后自然清空；Phase 1 不做持久化。
conversation_store = ConversationStore()
