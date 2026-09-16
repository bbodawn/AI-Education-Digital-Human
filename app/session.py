"""V0.1 的当前会话状态。

V0.1 只支持「一个浏览器 → 一个数字人会话」，所以 session_id 直接放在进程内存里。

这是刻意的临时实现：要支持多用户并发时，这个模块会被换成真正的会话存储
（Redis / 数据库等），而调用方（api 层）的代码形状不变——它们只用到
set_session_id / get_session_id 这两个动作。
"""

_current_session_id: str | int | None = None


def set_session_id(session_id: str | int) -> None:
    """记录当前会话。再次调用会覆盖，因此重连时自然复用同一位置。"""
    global _current_session_id
    _current_session_id = session_id


def get_session_id() -> str | int | None:
    """取当前会话，未建立时返回 None。"""
    return _current_session_id


def clear_session_id() -> None:
    """清空当前会话。"""
    global _current_session_id
    _current_session_id = None
