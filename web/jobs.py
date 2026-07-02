"""
web/jobs.py — Quản lý thư mục tạm cho upload/download và đóng gói ZIP.

- Upload: lưu file người dùng gửi lên vào <root>/<uid>/uploads/<upload_id>/input
- Download: tải ảnh về <root>/<uid>/downloads/<token>/<safe_name> rồi zip thành <token>.zip
- Mỗi ZIP gắn 1 download token; frontend gọi GET /api/dl/<token> để tải về.
"""
import os
import re
import time
import uuid
import shutil
import zipfile
import threading
from pathlib import Path

WEB_JOBS_DIR = Path(os.environ.get('AUTOFOTELLO_WEB_JOBS', str(Path.home() / '.autofotello' / 'web_jobs')))

# token -> {'path': <zip path>, 'name': <filename gợi ý>, 'ts': created}
_downloads: dict[str, dict] = {}
_lock = threading.Lock()

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp', '.bmp',
              '.cr2', '.cr3', '.nef', '.arw', '.dng', '.raf'}


def _uid_dir(uid: str) -> Path:
    safe = ''.join(c for c in str(uid) if c.isalnum() or c in '-_') or 'anon'
    return WEB_JOBS_DIR / safe


def safe_name(name: str, fallback: str = 'project') -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', '', str(name or '')).strip()
    return cleaned or fallback


# ── Upload ──

def new_upload_dir(uid: str) -> tuple[str, Path]:
    upload_id = uuid.uuid4().hex[:12]
    d = _uid_dir(uid) / 'uploads' / upload_id / 'input'
    d.mkdir(parents=True, exist_ok=True)
    return upload_id, d


def upload_input_dir(uid: str, upload_id: str) -> Path | None:
    safe_id = ''.join(c for c in str(upload_id) if c.isalnum())
    if not safe_id:
        return None
    d = _uid_dir(uid) / 'uploads' / safe_id / 'input'
    return d if d.is_dir() else None


def ensure_upload_dir(uid: str, upload_id: str) -> tuple[str, Path] | tuple[None, None]:
    """Như upload_input_dir nhưng tạo thư mục nếu chưa có.

    Dùng khi client tự sinh upload_id (để các lô upload đầu tiên có thể
    chạy song song ngay từ đầu thay vì phải đợi lô đầu tạo upload_id).
    """
    safe_id = ''.join(c for c in str(upload_id) if c.isalnum())[:40]
    if not safe_id:
        return None, None
    d = _uid_dir(uid) / 'uploads' / safe_id / 'input'
    d.mkdir(parents=True, exist_ok=True)
    return safe_id, d


def discard_upload(uid: str, upload_id: str) -> bool:
    """Xóa toàn bộ dữ liệu đã nhận của 1 lần upload lỗi giữa chừng (retry hết mà vẫn lỗi)."""
    safe_id = ''.join(c for c in str(upload_id) if c.isalnum())[:40]
    if not safe_id:
        return False
    d = _uid_dir(uid) / 'uploads' / safe_id
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False


def list_upload_files(uid: str, upload_id: str) -> list[str]:
    d = upload_input_dir(uid, upload_id)
    if not d:
        return []
    return sorted(str(p) for p in d.iterdir()
                  if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


# ── Download / ZIP ──

def new_download_workdir(uid: str) -> tuple[str, Path]:
    """Tạo thư mục làm việc cho 1 lần tải 1 project. Trả (token, dir)."""
    token = uuid.uuid4().hex
    d = _uid_dir(uid) / 'downloads' / token / 'files'
    d.mkdir(parents=True, exist_ok=True)
    return token, d


def zip_workdir(token: str, work_dir: Path, zip_basename: str) -> str | None:
    """Nén work_dir thành <token>.zip cạnh nó. Trả URL-token nếu có file, None nếu rỗng."""
    work_dir = Path(work_dir)
    files = [p for p in work_dir.rglob('*') if p.is_file()]
    if not files:
        return None
    zip_path = work_dir.parent / f'{token}.zip'
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, arcname=p.relative_to(work_dir))
    with _lock:
        _downloads[token] = {
            'path': str(zip_path),
            'name': f'{safe_name(zip_basename)}.zip',
            'ts': time.time(),
        }
    # Dọn ảnh gốc, chỉ giữ file zip.
    try:
        shutil.rmtree(work_dir, ignore_errors=True)
    except Exception:
        pass
    return token


def get_download(token: str) -> dict | None:
    safe = ''.join(c for c in str(token) if c.isalnum())
    with _lock:
        info = _downloads.get(safe)
    if not info:
        return None
    if not os.path.exists(info['path']):
        with _lock:
            _downloads.pop(safe, None)
        return None
    return info


def _tree_mtime(path: Path) -> float:
    """mtime mới nhất của path (và toàn bộ file con) — để không xóa nhầm job đang chạy."""
    try:
        latest = path.stat().st_mtime
    except Exception:
        return 0.0
    if path.is_dir():
        for p in path.rglob('*'):
            try:
                latest = max(latest, p.stat().st_mtime)
            except Exception:
                pass
    return latest


def cleanup(max_age_sec: int = 3600) -> int:
    """Xóa mọi thư mục upload + download (zip) quá 'max_age_sec' giây (theo mtime mới nhất).

    Trả về số mục đã xóa. An toàn: job đang chạy có file mới ghi -> mtime mới -> không bị xóa.
    """
    now = time.time()
    removed = 0
    if not WEB_JOBS_DIR.exists():
        return 0
    try:
        uid_dirs = list(WEB_JOBS_DIR.iterdir())
    except Exception:
        return 0
    for uid_dir in uid_dirs:
        if not uid_dir.is_dir():
            continue
        for sub in ('uploads', 'downloads'):
            base = uid_dir / sub
            if not base.is_dir():
                continue
            for item in list(base.iterdir()):
                try:
                    if now - _tree_mtime(item) <= max_age_sec:
                        continue
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
                    removed += 1
                except Exception:
                    pass
    # Dọn registry token đã mất file zip
    with _lock:
        for t in list(_downloads):
            if not os.path.exists(_downloads[t].get('path', '')):
                _downloads.pop(t, None)
    return removed
