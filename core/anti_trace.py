"""
core/anti_trace.py — Phát hiện công cụ trace/MITM (HTTP Toolkit, Fiddler, Charles, Burp,
mitmproxy, Wireshark...). Nhằm chống mổ xẻ traffic trên máy local.

Lưu ý: đây là DETERRENT (nâng rào cản), không phải bảo mật tuyệt đối — user toàn quyền
trên máy của họ. Bảo mật thật nằm ở RLS/RPC phía Supabase.
"""
import sys
import logging
import subprocess

logger = logging.getLogger(__name__)

# (Tên hiển thị, các chuỗi con xuất hiện trong tên tiến trình - viết thường)
_INTERCEPT_TOOLS = [
    ('HTTP Toolkit', ['httptoolkit', 'http toolkit', 'http-toolkit']),
    ('Fiddler',      ['fiddler']),
    ('Charles Proxy',['charles']),
    ('Burp Suite',   ['burpsuite', 'burp suite', 'burpsuitecommunity', 'burpsuitepro']),
    ('mitmproxy',    ['mitmproxy', 'mitmweb', 'mitmdump']),
    ('Wireshark',    ['wireshark']),
    ('Proxyman',     ['proxyman']),
    ('Charles',      ['charlesproxy']),
]


def _running_processes() -> str:
    """Trả về 1 chuỗi (lowercase) chứa tên tất cả tiến trình đang chạy."""
    try:
        if sys.platform == 'win32':
            # tasklist nhanh, có sẵn trên mọi Windows
            out = subprocess.check_output(
                ['tasklist', '/fo', 'csv', '/nh'],
                text=True, errors='ignore',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        else:
            out = subprocess.check_output(['ps', '-axco', 'command'], text=True, errors='ignore')
        return out.lower()
    except Exception as e:
        logger.debug(f'anti_trace: không liệt kê được tiến trình: {e}')
        return ''


def detect_interception_tools() -> list:
    """Trả về danh sách tên tool MITM/trace đang chạy (rỗng nếu không có)."""
    procs = _running_processes()
    if not procs:
        return []
    found = []
    for name, needles in _INTERCEPT_TOOLS:
        if any(n in procs for n in needles):
            found.append(name)
    return found


def is_being_traced() -> bool:
    return bool(detect_interception_tools())
