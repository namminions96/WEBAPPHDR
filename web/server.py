"""
web/server.py — Flask web port của AutoFotello (đa người dùng).

Thay lớp cầu nối pywebview (window.pywebview.api.*) bằng REST API + SSE.
Giữ nguyên nghiệp vụ upload/download/login của engines Fotello & AutoHDR.
"""
import os
import re
import json
import time
import uuid
import logging
import threading
from functools import wraps
from pathlib import Path
from datetime import timedelta

from urllib.parse import quote

from flask import (
    Flask, request, session, jsonify, send_file, redirect, url_for,
    Response, stream_with_context, render_template,
)

from core.config import CURRENT_VERSION
from core.session import session_manager, log_bus
from engines import fotello_engine, autohdr_engine

from web import auth_web, user_store, jobs
from web.user_store import WEB_DATA_DIR

logger = logging.getLogger(__name__)


def _load_secret() -> str:
    """Secret key CỐ ĐỊNH để cookie đăng nhập không mất khi restart server.

    Ưu tiên env AUTOFOTELLO_SECRET; nếu không có thì lưu/đọc từ file .flask_secret.
    """
    s = os.environ.get('AUTOFOTELLO_SECRET')
    if s:
        return s
    try:
        WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)
        fp = WEB_DATA_DIR / '.flask_secret'
        if fp.exists():
            v = fp.read_text(encoding='utf-8').strip()
            if v:
                return v
        v = uuid.uuid4().hex
        fp.write_text(v, encoding='utf-8')
        return v
    except Exception:
        return uuid.uuid4().hex


# Dùng trust store OS cho SSL (an toàn khi có/không proxy). Idempotent.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = _load_secret()
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('AUTOFOTELLO_MAX_UPLOAD', 4 * 1024 * 1024 * 1024))
app.permanent_session_lifetime = timedelta(days=int(os.environ.get('AUTOFOTELLO_SESSION_DAYS', '7')))
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0   # không cache tĩnh lâu -> luôn nạp JS/CSS mới
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
if os.environ.get('AUTOFOTELLO_HTTPS', '').lower() in ('1', 'true', 'yes'):
    app.config['SESSION_COOKIE_SECURE'] = True

# Chạy sau reverse proxy (nginx): tin X-Forwarded-* để url_for(_external) ra đúng https/host.
if os.environ.get('AUTOFOTELLO_PROXY', '').lower() in ('1', 'true', 'yes'):
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Phiên đăng nhập lưu server-side (tránh cookie phình vì JWT). Cookie chỉ giữ 'sid'.
# Ghi thêm ra đĩa để sống sót qua restart server (auto-login khi reload trình duyệt).
_SESSIONS: dict[str, dict] = {}
_sess_lock = threading.Lock()
_SESS_DIR = WEB_DATA_DIR / '_sessions'


def _sess_file(sid: str) -> Path:
    safe = ''.join(c for c in str(sid) if c.isalnum())
    return _SESS_DIR / f'{safe}.json'


def _persist_session(sid: str, data: dict) -> None:
    try:
        _SESS_DIR.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in data.items() if k != 'tasks'}
        _sess_file(sid).write_text(json.dumps(payload), encoding='utf-8')
    except Exception as e:
        logger.warning(f'Không lưu được session ra đĩa: {e}')


# ──────────────────────────────────────────────────────────────
# Session helpers
# ──────────────────────────────────────────────────────────────

def _current():
    sid = session.get('sid')
    if not sid:
        return None
    with _sess_lock:
        s = _SESSIONS.get(sid)
    if s is not None:
        return s
    # Không có trong RAM (vd sau khi restart server) -> nạp lại từ đĩa
    fp = _sess_file(sid)
    if fp.exists():
        try:
            data = json.loads(fp.read_text(encoding='utf-8'))
            data['tasks'] = set()
            with _sess_lock:
                _SESSIONS[sid] = data
            return data
        except Exception:
            return None
    return None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = _current()
        if not u:
            return jsonify({'ok': False, 'msg': 'Chưa đăng nhập', 'auth': False}), 401
        # Hết hạn cục bộ
        from core.supabase_auth import is_user_expired
        if is_user_expired(u.get('expire_at')):
            _destroy_session()
            return jsonify({'ok': False, 'msg': 'Tài khoản đã hết hạn', 'auth': False}), 401
        return fn(*args, **kwargs)
    return wrapper


# Phân quyền dịch vụ theo role — KHỚP với hasAccess() ở frontend (app.web.js).
_ROLE_MAP = {
    'hdr': 'autohdr', 'autohdr': 'autohdr',
    'flo': 'fotello', 'fotello': 'fotello',
    'upcase': 'upcase', 'upscale': 'upcase',
    'enhance': 'autoenhance', 'ae': 'autoenhance', 'autoenhance': 'autoenhance',
}


def _has_access(role: str, svc: str) -> bool:
    r = (role or '').lower().strip()
    tokens = [t for t in re.split(r'[\s,+|]+', r) if t]
    granted = [_ROLE_MAP[t] for t in tokens if t in _ROLE_MAP]
    if granted:
        return svc in granted
    # role 'ALL'/'admin'/'member'/không khớp token nào -> full quyền (giữ tương thích)
    return True


def service_required(svc: str = None):
    """Chặn phía server theo quyền dịch vụ. svc=None -> đọc từ route param <svc>."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            u = _current()
            s = svc or kwargs.get('svc')
            if u and s in ('fotello', 'autohdr', 'upcase', 'autoenhance') \
                    and not _has_access(u.get('role'), s):
                return jsonify({'ok': False,
                                'msg': f'Tài khoản của bạn không có quyền dùng {s.upper()}.'}), 403
            return fn(*args, **kwargs)
        return wrapper
    return deco


def _create_session(data: dict) -> None:
    sid = uuid.uuid4().hex
    data['tasks'] = set()
    with _sess_lock:
        _SESSIONS[sid] = data
    _persist_session(sid, data)
    session['sid'] = sid
    session.permanent = True


def _destroy_session() -> None:
    sid = session.pop('sid', None)
    if sid:
        with _sess_lock:
            _SESSIONS.pop(sid, None)
        try:
            _sess_file(sid).unlink(missing_ok=True)
        except Exception:
            pass


def _uid() -> str:
    u = _current() or {}
    return u.get('user_id', 'anon')


def _track_task(sid: str) -> None:
    u = _current()
    if u is not None:
        u['tasks'].add(sid)


# ──────────────────────────────────────────────────────────────
# Log/progress bus helpers (web)
# ──────────────────────────────────────────────────────────────

def _bus_start(sid: str):
    log_bus.create(sid)

    def _log(msg):
        log_bus.put(sid, {'type': 'log', 'msg': msg})

    def _prog(cur, total):
        pct = int(cur * 100 / total) if total else 0
        log_bus.put(sid, {'type': 'progress', 'current': cur, 'total': total, 'pct': pct})

    return _log, _prog


def _bus_done(sid: str):
    log_bus.put(sid, {'type': 'done'})
    # Giữ queue thêm ít giây cho SSE lấy nốt rồi mới xóa.
    def _cleanup():
        time.sleep(15)
        log_bus.remove(sid)
    threading.Thread(target=_cleanup, daemon=True).start()


# ──────────────────────────────────────────────────────────────
# Pages
# ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    # asset_ver đổi mỗi khi app.web.js thay đổi -> ép trình duyệt/Cloudflare nạp bản mới (bust cache).
    try:
        asset_ver = int(os.path.getmtime(os.path.join(app.static_folder, 'app.web.js')))
    except Exception:
        asset_ver = CURRENT_VERSION
    return render_template('index.html', version=CURRENT_VERSION, asset_ver=asset_ver)


# ──────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────

@app.route('/api/login', methods=['POST'])
def api_login():
    body = request.get_json(silent=True) or {}
    email = (body.get('username') or body.get('email') or '').strip()
    password = body.get('password') or ''
    if not email or not password:
        return jsonify({'ok': False, 'msg': 'Vui lòng nhập tài khoản và mật khẩu!'})
    res = auth_web.web_login(email, password)
    if not res.get('ok'):
        return jsonify(res)
    _create_session({
        'user_id': res['user_id'],
        'email': res['email'],
        'role': res['role'],
        'expire_at': res['expire_at'],
        'access_token': res['access_token'],
        'refresh_token': res['refresh_token'],
    })
    return jsonify({
        'ok': True, 'username': res['email'], 'email': res['email'],
        'role': res['role'], 'expire_at': res['expire_at'],
    })


@app.route('/api/login/google', methods=['GET'])
def api_login_google():
    redirect_to = url_for('auth_callback', _external=True)
    oauth_url = auth_web.google_start(redirect_to)
    if not oauth_url:
        return redirect('/?login_error=' + quote('Không khởi tạo được đăng nhập Google. Kiểm tra cấu hình OAuth.'))
    return redirect(oauth_url)


@app.route('/auth/callback', methods=['GET'])
def auth_callback():
    err = request.args.get('error_description')
    if err:
        return redirect('/?login_error=' + quote(err))
    code = request.args.get('code')
    if not code:
        return redirect('/?login_error=' + quote('Thiếu mã xác thực từ Google.'))
    res = auth_web.google_finish(code)
    if not res.get('ok'):
        return redirect('/?login_error=' + quote(res.get('msg', 'Đăng nhập Google thất bại.')))
    _create_session({
        'user_id': res['user_id'], 'email': res['email'], 'role': res['role'],
        'expire_at': res['expire_at'], 'access_token': res['access_token'],
        'refresh_token': res['refresh_token'],
    })
    return redirect('/')


@app.route('/api/logout', methods=['POST'])
def api_logout():
    _destroy_session()
    return jsonify({'ok': True})


@app.route('/api/me', methods=['GET'])
def api_me():
    u = _current()
    if not u:
        return jsonify({'ok': False})
    from core.supabase_auth import is_user_expired
    if is_user_expired(u.get('expire_at')):
        _destroy_session()
        return jsonify({'ok': False, 'msg': 'Tài khoản đã hết hạn sử dụng!'})

    # Kiểm tra LIVE với Supabase (khóa tài khoản / đổi role / hết hạn server-side).
    # Throttle 30s để reload liên tục không gọi Supabase mỗi lần.
    now = time.time()
    if now - u.get('_last_validated', 0) > 30:
        res = auth_web.web_validate(u.get('access_token', ''), u.get('refresh_token', ''))
        if not res.get('ok'):
            _destroy_session()
            return jsonify({'ok': False, 'msg': res.get('msg', 'Phiên đăng nhập không hợp lệ!')})
        if not res.get('transient'):
            u['role'] = res.get('role', u.get('role'))
            u['expire_at'] = res.get('expire_at', u.get('expire_at'))
            if res.get('access_token'):
                u['access_token'] = res['access_token']
            if res.get('refresh_token'):
                u['refresh_token'] = res['refresh_token']
            u['_last_validated'] = now
            _persist_session(session.get('sid'), u)

    return jsonify({
        'ok': True, 'username': u['email'], 'email': u['email'],
        'role': u['role'], 'expire_at': u['expire_at'],
    })


# ──────────────────────────────────────────────────────────────
# Connect Fotello / AutoHDR (dán token/cookie thủ công)
# ──────────────────────────────────────────────────────────────

@app.route('/api/fotello/connect', methods=['POST'])
@login_required
@service_required('fotello')
def api_fotello_connect():
    body = request.get_json(silent=True) or {}
    token = (body.get('refresh_token') or '').strip()
    if not token:
        return jsonify({'ok': False, 'msg': 'Thiếu refresh_token'})
    state = fotello_engine.fotello_state_from_refresh_token(token)
    if not state:
        return jsonify({'ok': False, 'msg': 'Token Fotello không hợp lệ hoặc đã hết hạn.'})
    user_store.save_fotello(_uid(), state)
    return jsonify({'ok': True})


@app.route('/api/autohdr/connect', methods=['POST'])
@login_required
@service_required('autohdr')
def api_autohdr_connect():
    body = request.get_json(silent=True) or {}
    cookie = (body.get('cookie') or '').strip()
    if not cookie:
        return jsonify({'ok': False, 'msg': 'Thiếu cookie'})
    valid, reason = autohdr_engine._validate_autohdr_cookie(cookie)
    if not valid:
        return jsonify({'ok': False, 'msg': f'Cookie AutoHDR không hợp lệ ({reason}).'})
    user_store.save_autohdr(_uid(), cookie)
    return jsonify({'ok': True})


@app.route('/api/<svc>/status', methods=['GET'])
@login_required
def api_status(svc):
    uid = _uid()
    if svc == 'fotello':
        return jsonify({'connected': user_store.load_fotello_raw(uid) is not None})
    if svc == 'autohdr':
        cookie = user_store.load_autohdr(uid)
        if not cookie:
            return jsonify({'connected': False})
        valid, reason = autohdr_engine._validate_autohdr_cookie(cookie)
        if not valid and reason.startswith('HTTP 4'):
            user_store.clear_autohdr(uid)
            return jsonify({'connected': False})
        return jsonify({'connected': True})
    return jsonify({'connected': False})


@app.route('/api/<svc>/disconnect', methods=['POST'])
@login_required
def api_disconnect(svc):
    uid = _uid()
    if svc == 'fotello':
        user_store.clear_fotello(uid)
    elif svc == 'autohdr':
        user_store.clear_autohdr(uid)
    return jsonify({'ok': True})


# ──────────────────────────────────────────────────────────────
# Listings
# ──────────────────────────────────────────────────────────────

@app.route('/api/fotello/listings', methods=['GET'])
@login_required
@service_required('fotello')
def api_fotello_listings():
    raw = user_store.load_fotello_raw(_uid())
    if not raw:
        return jsonify({'ok': False, 'listings': [], 'msg': 'Chưa kết nối Fotello'})
    data = fotello_engine.fotello_list_listings(state=raw)
    user_store.save_fotello(_uid(), raw)  # lưu lại token đã refresh
    return jsonify({'ok': True, 'listings': data})


@app.route('/api/autohdr/listings', methods=['GET'])
@login_required
@service_required('autohdr')
def api_autohdr_listings():
    cookie = user_store.load_autohdr(_uid())
    if not cookie:
        return jsonify({'ok': False, 'listings': [], 'msg': 'Chưa kết nối AutoHDR'})
    data = autohdr_engine.autohdr_list_listings(cookie, log_fn=lambda m: None)
    return jsonify({'ok': True, 'listings': data})


# ──────────────────────────────────────────────────────────────
# Upload files (multipart) → temp dir
# ──────────────────────────────────────────────────────────────

@app.route('/api/upload-files', methods=['POST'])
@login_required
def api_upload_files():
    files = request.files.getlist('files')
    if not files:
        return jsonify({'ok': False, 'msg': 'Không có file'})

    # Client tự sinh upload_id và gửi kèm ngay từ lô đầu -> mọi lô có thể chạy
    # song song ngay từ đầu (không cần đợi 1 lô "mở màn" để lấy upload_id).
    upload_id = (request.form.get('upload_id') or '').strip()
    input_dir = None
    if upload_id:
        upload_id, input_dir = jobs.ensure_upload_dir(_uid(), upload_id)
    if input_dir is None:
        upload_id, input_dir = jobs.new_upload_dir(_uid())

    saved = []
    for f in files:
        name = os.path.basename(f.filename or '')
        if not name:
            continue
        # Giữ tên gốc (kể cả unicode) nhưng chặn path traversal.
        name = name.replace('/', '_').replace('\\', '_')
        dest = input_dir / name
        f.save(str(dest))
        saved.append(name)
    return jsonify({'ok': True, 'upload_id': upload_id, 'files': saved})


@app.route('/api/upload-discard', methods=['POST'])
@login_required
def api_upload_discard():
    """Xóa toàn bộ file đã nhận của 1 upload_id lỗi giữa chừng (client gọi khi retry hết mà vẫn lỗi)."""
    upload_id = (request.form.get('upload_id') or '').strip()
    if not upload_id:
        return jsonify({'ok': False, 'msg': 'Thiếu upload_id'})
    jobs.discard_upload(_uid(), upload_id)
    return jsonify({'ok': True})


@app.route('/api/upload-chunk', methods=['POST'])
@login_required
def api_upload_chunk():
    """Nhận 1 mảnh của file lớn (để vượt giới hạn 100MB/request của Cloudflare).

    Các mảnh gửi TUẦN TỰ; ghi nối vào <filename>.part, mảnh cuối thì đổi tên thành file thật.
    """
    filename = os.path.basename(request.form.get('filename', '') or '').replace('/', '_').replace('\\', '_')
    chunk = request.files.get('chunk')
    if not filename or chunk is None:
        return jsonify({'ok': False, 'msg': 'Thiếu dữ liệu mảnh'})
    try:
        idx = int(request.form.get('chunk_index', '0'))
        total = int(request.form.get('total_chunks', '1'))
    except ValueError:
        return jsonify({'ok': False, 'msg': 'Tham số mảnh không hợp lệ'})

    upload_id = (request.form.get('upload_id') or '').strip()
    input_dir = jobs.upload_input_dir(_uid(), upload_id) if upload_id else None
    if input_dir is None:
        upload_id, input_dir = jobs.new_upload_dir(_uid())

    part = input_dir / (filename + '.part')
    with open(part, 'wb' if idx == 0 else 'ab') as fh:
        fh.write(chunk.read())

    if idx + 1 >= total:
        final = input_dir / filename
        if final.exists():
            final.unlink()
        part.rename(final)
    return jsonify({'ok': True, 'upload_id': upload_id})


# ──────────────────────────────────────────────────────────────
# Fotello: analyze brackets
# ──────────────────────────────────────────────────────────────

@app.route('/api/fotello/analyze-brackets', methods=['POST'])
@login_required
@service_required('fotello')
def api_fotello_analyze():
    body = request.get_json(silent=True) or {}
    upload_id = body.get('upload_id', '')
    bracket_size = int(body.get('bracket_size') or 3)
    mode = body.get('mode') or 'filename'
    input_dir = jobs.upload_input_dir(_uid(), upload_id)
    if not input_dir:
        return jsonify({'ok': False, 'msg': 'upload_id không hợp lệ hoặc đã hết hạn'})
    res = fotello_engine.fotello_analyze_brackets(str(input_dir), bracket_size=bracket_size, grouping_mode=mode)
    return jsonify(res)


# ──────────────────────────────────────────────────────────────
# Uploads (chạy nền, log qua SSE)
# ──────────────────────────────────────────────────────────────

@app.route('/api/fotello/upload', methods=['POST'])
@login_required
@service_required('fotello')
def api_fotello_upload():
    body = request.get_json(silent=True) or {}
    upload_id = body.get('upload_id', '')
    prefs = body.get('prefs') or {}
    brackets = body.get('brackets') or None
    sid = body.get('sid') or uuid.uuid4().hex[:8]
    uid = _uid()

    input_dir = jobs.upload_input_dir(uid, upload_id)
    if not input_dir:
        return jsonify({'ok': False, 'msg': 'upload_id không hợp lệ'})
    raw = user_store.load_fotello_raw(uid)
    if not raw:
        return jsonify({'ok': False, 'msg': 'Chưa kết nối Fotello'})

    _log, _prog = _bus_start(sid)
    stop_event = threading.Event()
    session_manager.add(sid, stop_event, 'upload')
    _track_task(sid)

    def _run():
        try:
            res = fotello_engine.fotello_upload_and_enhance(
                str(input_dir), str(input_dir.parent / 'out'),
                log=_log, progress_fn=_prog, is_cancelled=stop_event.is_set,
                preferences=prefs, predefined_brackets=brackets, state=raw,
            )
            user_store.save_fotello(uid, raw)
            _log(f"{'✅' if res.get('ok') else '❌'} {res.get('msg', '')}")
        except Exception as e:
            _log(f'❌ {e}')
        finally:
            session_manager.remove(sid)
            _bus_done(sid)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'ok': True, 'sid': sid})


@app.route('/api/autohdr/upload', methods=['POST'])
@login_required
@service_required('autohdr')
def api_autohdr_upload():
    body = request.get_json(silent=True) or {}
    upload_id = body.get('upload_id', '')
    address = body.get('address') or ''
    options = body.get('options') or {}
    sid = body.get('sid') or uuid.uuid4().hex[:8]
    uid = _uid()

    input_dir = jobs.upload_input_dir(uid, upload_id)
    if not input_dir:
        return jsonify({'ok': False, 'msg': 'upload_id không hợp lệ'})
    cookie = user_store.load_autohdr(uid)
    if not cookie:
        return jsonify({'ok': False, 'msg': 'Chưa kết nối AutoHDR'})

    _log, _prog = _bus_start(sid)
    stop_event = threading.Event()
    session_manager.add(sid, stop_event, 'upload')
    _track_task(sid)

    def _run():
        try:
            res = autohdr_engine.autohdr_upload_and_enhance(
                cookie, str(input_dir), str(input_dir.parent / 'out'), address,
                log_fn=_log, progress_fn=_prog, stop_event=stop_event, options=options,
            )
            _log(f"{'✅' if res.get('ok') else '❌'} {res.get('msg', '')}")
        except Exception as e:
            _log(f'❌ {e}')
        finally:
            session_manager.remove(sid)
            _bus_done(sid)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'ok': True, 'sid': sid})


# ──────────────────────────────────────────────────────────────
# Downloads → ZIP mỗi project
# ──────────────────────────────────────────────────────────────

@app.route('/api/<svc>/download', methods=['POST'])
@login_required
@service_required()
def api_download(svc):
    if svc not in ('fotello', 'autohdr'):
        return jsonify({'ok': False, 'msg': 'Dịch vụ không hợp lệ'})
    body = request.get_json(silent=True) or {}
    projects = body.get('projects') or []
    sid = body.get('sid') or uuid.uuid4().hex[:8]
    uid = _uid()

    if svc == 'fotello':
        raw = user_store.load_fotello_raw(uid)
        if not raw:
            return jsonify({'ok': False, 'msg': 'Chưa kết nối Fotello'})
        creds = raw
    else:
        cookie = user_store.load_autohdr(uid)
        if not cookie:
            return jsonify({'ok': False, 'msg': 'Chưa kết nối AutoHDR'})
        creds = cookie

    _log, _prog = _bus_start(sid)
    stop_event = threading.Event()
    session_manager.add(sid, stop_event, 'download')
    _track_task(sid)

    def _run():
        try:
            n = len(projects)
            for i, proj in enumerate(projects):
                if stop_event.is_set():
                    break
                proj_id = str(proj.get('id'))
                proj_name = proj.get('name') or proj_id or 'project'
                safe = jobs.safe_name(proj_name, proj_id)
                _log(f"Đang tải dự án '{proj_name}'…")
                token, work_dir = jobs.new_download_workdir(uid)
                if svc == 'fotello':
                    fotello_engine.fotello_download_listing(
                        proj_id, str(work_dir), state=creds, log=_log,
                        is_cancelled=stop_event.is_set,
                    )
                else:
                    autohdr_engine.run_clean_download(
                        creds, proj_id, str(work_dir), log_fn=_log,
                        progress_fn=lambda c, t: None, stop_event=stop_event,
                        create_subfolder=False,
                    )
                zip_token = jobs.zip_workdir(token, work_dir, safe)
                if zip_token:
                    _log(f"✅ Đã đóng gói '{safe}.zip'")
                    log_bus.put(sid, {
                        'type': 'download_ready',
                        'name': f'{safe}.zip',
                        'url': f'/api/dl/{zip_token}',
                    })
                else:
                    _log(f"⚠ '{proj_name}' không có ảnh để tải")
                _prog(i + 1, n)
            if svc == 'fotello':
                user_store.save_fotello(uid, creds)
            _log('✅ Hoàn tất tải về')
        except Exception as e:
            _log(f'❌ {e}')
        finally:
            session_manager.remove(sid)
            _bus_done(sid)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'ok': True, 'sid': sid})


@app.route('/api/dl/<token>', methods=['GET'])
@login_required
def api_dl(token):
    info = jobs.get_download(token)
    if not info:
        return jsonify({'ok': False, 'msg': 'File không tồn tại hoặc đã hết hạn'}), 404
    return send_file(info['path'], as_attachment=True, download_name=info['name'])


# ──────────────────────────────────────────────────────────────
# Stop
# ──────────────────────────────────────────────────────────────

@app.route('/api/stop', methods=['POST'])
@login_required
def api_stop():
    u = _current()
    for sid in list(u.get('tasks', [])):
        session_manager.cancel(sid)
    return jsonify({'ok': True})


# ──────────────────────────────────────────────────────────────
# SSE events
# ──────────────────────────────────────────────────────────────

@app.route('/api/events/<sid>', methods=['GET'])
@login_required
def api_events(sid):
    def gen():
        # đảm bảo queue tồn tại để không mất event đầu
        idle = 0
        while True:
            msgs = log_bus.drain(sid)
            if msgs:
                idle = 0
                for m in msgs:
                    yield f'data: {json.dumps(m)}\n\n'
                    if isinstance(m, dict) and m.get('type') == 'done':
                        return
            else:
                idle += 1
                # heartbeat để giữ kết nối
                yield ': ping\n\n'
                if idle > 600:  # ~5 phút không có gì -> đóng
                    return
            time.sleep(0.5)

    resp = Response(stream_with_context(gen()), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Connection'] = 'keep-alive'
    return resp


# ──────────────────────────────────────────────────────────────
# UpCase / AutoEnhance — chưa hỗ trợ trên web
# ──────────────────────────────────────────────────────────────

@app.route('/api/upcase/<path:_any>', methods=['GET', 'POST'])
@app.route('/api/autoenhance/<path:_any>', methods=['GET', 'POST'])
@login_required
def api_not_supported(_any):
    return jsonify({'ok': False, 'msg': 'Dịch vụ này chưa hỗ trợ trên bản web.'})


# ──────────────────────────────────────────────────────────────
# Error handlers — luôn trả JSON cho /api để frontend không vỡ khi parse
# ──────────────────────────────────────────────────────────────

@app.errorhandler(413)
def _err_too_large(e):
    return jsonify({
        'ok': False,
        'msg': 'File tải lên quá lớn (HTTP 413). Tăng giới hạn upload trên server (nginx client_max_body_size).'
    }), 413


@app.errorhandler(500)
def _err_internal(e):
    if request.path.startswith('/api/'):
        return jsonify({'ok': False, 'msg': 'Lỗi máy chủ (HTTP 500).'}), 500
    return e


# ──────────────────────────────────────────────────────────────
# Janitor — tự xóa file upload/download quá hạn (mặc định 1h)
# ──────────────────────────────────────────────────────────────

def _start_janitor():
    ttl = int(os.environ.get('AUTOFOTELLO_FILE_TTL', '3600'))   # 1h
    interval = max(60, min(ttl, 600))                            # quét mỗi ≤10 phút

    def _loop():
        while True:
            time.sleep(interval)
            try:
                n = jobs.cleanup(ttl)
                if n:
                    logger.info(f'[janitor] đã xóa {n} thư mục/file tạm quá {ttl}s')
            except Exception as e:
                logger.warning(f'[janitor] lỗi: {e}')

    threading.Thread(target=_loop, daemon=True, name='janitor').start()
    logger.info(f'[janitor] bật — TTL={ttl}s, quét mỗi {interval}s')


_start_janitor()


def create_app():
    return app
