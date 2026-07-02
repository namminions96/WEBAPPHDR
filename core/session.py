"""
core/session.py — Quản lý session và log message bus
"""
import queue
import threading
import time


class SessionManager:
    """Theo dõi các task đang chạy, cho phép cancel theo session_id."""

    def __init__(self):
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()

    def add(self, session_id: str, stop_event: threading.Event, task_type: str = 'upload') -> None:
        with self._lock:
            self._sessions[session_id] = {
                'stop_event': stop_event,
                'task_type': task_type,
                'started': time.time(),
            }

    def remove(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def cancel(self, session_id: str) -> bool:
        with self._lock:
            s = self._sessions.get(session_id)
        if s:
            s['stop_event'].set()
            return True
        return False

    def cancel_all(self) -> None:
        with self._lock:
            for s in self._sessions.values():
                s['stop_event'].set()

    def cleanup_finished(self) -> None:
        with self._lock:
            done = [sid for sid, s in self._sessions.items() if s['stop_event'].is_set()]
            for sid in done:
                self._sessions.pop(sid, None)

    def active_ids(self) -> list[str]:
        with self._lock:
            return list(self._sessions.keys())


class LogBus:
    """
    Queue-based pub/sub bus để engine gửi log/progress tới GUI.
    GUI poll mỗi 100ms qua after().
    """

    def __init__(self):
        self._queues: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()

    def create(self, session_id: str) -> None:
        with self._lock:
            self._queues[session_id] = queue.Queue()

    def put_log(self, session_id: str, msg: str) -> None:
        with self._lock:
            q = self._queues.get(session_id)
        if q:
            q.put(('log', msg))

    def put(self, session_id: str, item) -> None:
        """Đẩy 1 event bất kỳ (dict) vào queue — dùng cho web SSE."""
        with self._lock:
            q = self._queues.get(session_id)
        if q:
            q.put(item)

    def put_progress(self, session_id: str, current: int, total: int) -> None:
        with self._lock:
            q = self._queues.get(session_id)
        if q:
            pct = int(current * 100 / total) if total > 0 else 0
            q.put(('progress', current, total, pct))

    def drain(self, session_id: str) -> list:
        """Lấy tất cả messages còn trong queue, không block."""
        with self._lock:
            q = self._queues.get(session_id)
        if not q:
            return []
        msgs = []
        try:
            while True:
                msgs.append(q.get_nowait())
        except queue.Empty:
            pass
        return msgs

    def remove(self, session_id: str) -> None:
        with self._lock:
            self._queues.pop(session_id, None)


# Singleton instances dùng trong toàn app
session_manager = SessionManager()
log_bus = LogBus()
