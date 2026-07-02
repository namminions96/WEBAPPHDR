"""
web/user_store.py — Lưu trữ token/cookie kết nối Fotello/AutoHDR THEO TỪNG USER.

Khác desktop (1 file token chung trong ~/.autofotello), bản web đa người dùng lưu riêng
theo Supabase user id, để token của user này không đè lên user khác.
"""
import os
import json
import threading
from pathlib import Path

from core.config import APP_DATA_DIR

# Thư mục gốc lưu dữ liệu web, tách khỏi file token desktop.
WEB_DATA_DIR = Path(os.environ.get('AUTOFOTELLO_WEB_DATA', str(APP_DATA_DIR / 'web_users')))

_lock = threading.Lock()


def _user_dir(uid: str) -> Path:
    # uid từ Supabase là UUID an toàn; vẫn chặn path traversal cho chắc.
    safe = ''.join(c for c in str(uid) if c.isalnum() or c in '-_')
    d = WEB_DATA_DIR / (safe or 'anon')
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fotello_file(uid: str) -> Path:
    return _user_dir(uid) / 'fotello_tokens.json'


def _autohdr_file(uid: str) -> Path:
    return _user_dir(uid) / 'autohdr_cookie.json'


# ── Fotello ──

def save_fotello(uid: str, state: dict) -> None:
    with _lock:
        with open(_fotello_file(uid), 'w', encoding='utf-8') as f:
            json.dump({
                'refresh_token': state.get('refresh_token', ''),
                'id_token': state.get('id_token', ''),
                'access_token': state.get('access_token', ''),
                'team_id': state.get('team_id', ''),
            }, f)


def load_fotello_raw(uid: str) -> dict | None:
    """Đọc token đã lưu (chưa refresh). Trả None nếu chưa kết nối."""
    fp = _fotello_file(uid)
    if not fp.exists():
        return None
    try:
        with open(fp, encoding='utf-8') as f:
            data = json.load(f)
        if not data.get('refresh_token'):
            return None
        return {
            'refresh_token': data.get('refresh_token', ''),
            'id_token': data.get('id_token', ''),
            'access_token': data.get('access_token', ''),
            'team_id': data.get('team_id', ''),
            'connected': True,
        }
    except Exception:
        return None


def clear_fotello(uid: str) -> None:
    with _lock:
        fp = _fotello_file(uid)
        if fp.exists():
            fp.unlink()


# ── AutoHDR ──

def save_autohdr(uid: str, cookie: str) -> None:
    with _lock:
        with open(_autohdr_file(uid), 'w', encoding='utf-8') as f:
            json.dump({'cookie': cookie}, f)


def load_autohdr(uid: str) -> str:
    fp = _autohdr_file(uid)
    if not fp.exists():
        return ''
    try:
        with open(fp, encoding='utf-8') as f:
            return json.load(f).get('cookie', '')
    except Exception:
        return ''


def clear_autohdr(uid: str) -> None:
    with _lock:
        fp = _autohdr_file(uid)
        if fp.exists():
            fp.unlink()
