"""验证进程内短期对话历史的轮次、隔离、裁剪与锁语义。"""

from app.conversation import MAX_HISTORY_TURNS, ConversationStore


def test_empty_history_returns_no_messages() -> None:
    store = ConversationStore()

    assert store.get_messages("A") == []


def test_append_turn_returns_user_then_assistant() -> None:
    store = ConversationStore()

    store.append_turn("A", "什么是 RAG？", "RAG 是检索增强生成。")

    assert store.get_messages("A") == [
        {"role": "user", "content": "什么是 RAG？"},
        {"role": "assistant", "content": "RAG 是检索增强生成。"},
    ]


def test_multiple_turns_keep_message_order() -> None:
    store = ConversationStore()

    for index in range(1, 4):
        store.append_turn("A", f"U{index}", f"A{index}")

    assert store.get_messages("A") == [
        {"role": "user", "content": "U1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "U2"},
        {"role": "assistant", "content": "A2"},
        {"role": "user", "content": "U3"},
        {"role": "assistant", "content": "A3"},
    ]


def test_history_discards_oldest_turn_after_limit() -> None:
    store = ConversationStore()

    for index in range(1, MAX_HISTORY_TURNS + 2):
        store.append_turn("A", f"U{index}", f"A{index}")

    messages = store.get_messages("A")
    assert len(messages) == MAX_HISTORY_TURNS * 2
    assert messages[0] == {"role": "user", "content": "U2"}
    assert messages[-1] == {"role": "assistant", "content": "A6"}


def test_sessions_have_isolated_histories() -> None:
    store = ConversationStore()

    store.append_turn("A", "A 的问题", "A 的回答")

    assert store.get_messages("B") == []


def test_numeric_and_string_session_ids_share_history() -> None:
    store = ConversationStore()

    store.append_turn(1, "问题", "回答")

    assert store.get_messages("1") == [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "回答"},
    ]


def test_clear_only_removes_selected_session() -> None:
    store = ConversationStore()
    store.append_turn("A", "A 的问题", "A 的回答")
    store.append_turn("B", "B 的问题", "B 的回答")

    store.clear("A")

    assert store.get_messages("A") == []
    assert store.get_messages("B") == [
        {"role": "user", "content": "B 的问题"},
        {"role": "assistant", "content": "B 的回答"},
    ]


def test_lock_is_stable_per_normalized_session() -> None:
    store = ConversationStore()

    assert store.lock_for("A") is store.lock_for("A")
    assert store.lock_for(1) is store.lock_for("1")
    assert store.lock_for("A") is not store.lock_for("B")
