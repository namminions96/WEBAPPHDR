"""
engines/fotello_engine.py — Fotello Integration Engine
Copy & adapt từ AutoFotello_source/fotello_engine.py
Thay JS callbacks → log_callback(msg) / progress_callback(current, total)
"""
import re
import os
import json
import time
import base64
import logging
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from concurrent.futures import as_completed, ThreadPoolExecutor

import requests
from PIL import Image
import rawpy
import imagehash

logger = logging.getLogger(__name__)

FIREBASE_API_KEY  = 'AIzaSyA9NOX3S33RaSfpMp00rBHxRrC8n7rKA1o'
FIREBASE_AUTH_URL = f'https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}'
FIREBASE_PROJECT  = 'real-estate-firebase-4109e'
FIRESTORE_URL     = f'https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents'
FOTELLO_API       = 'https://api.fotello.co/v1'

_XOR_KEY  = b'Ft2026Obf'
MAX_RETRIES   = 5
POLL_INTERVAL = 5
POLL_TIMEOUT  = 1800

_FLD_SV            = 'stringValue'
_FLD_BV            = 'booleanValue'
_FLD_STATUS        = 'status'
_FLD_IS_WM         = 'isWatermarked'
_FLD_ENHANCES      = 'enhances'
_FLD_EDITED        = 'editedImage'
_FLD_EDITED_UPSIZED = 'editedImageUpsized'

_EP_CREATE_LISTING = 'create-listing'
_EP_CREATE_UPLOAD  = 'create-upload'
_EP_CREATE_ENHANCE = 'create-enhance'

CONTENT_TYPES = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.webp': 'image/webp', '.tiff': 'image/tiff', '.tif': 'image/tiff',
    '.bmp': 'image/bmp', '.cr3': 'image/x-canon-cr3', '.cr2': 'image/x-canon-cr2',
    '.nef': 'image/x-nikon-nef', '.arw': 'image/x-sony-arw', '.dng': 'image/x-adobe-dng',
}
IMAGE_EXTENSIONS = set(CONTENT_TYPES.keys())
TEAM_ID_CLAIM_KEYS = ('teamId', 'team_id', 'team', 'defaultTeamId', 'teamID')
TEAM_ID_USER_DOC_PATHS = (
    'users/{uid}', 'users_public/{uid}', 'user_profiles/{uid}',
    'profiles/{uid}', 'memberships/{uid}', 'team_members/{uid}',
)

_fotello_state = {
    'refresh_token': '',
    'id_token': '',
    'access_token': '',
    'team_id': '',
    'connected': False,
}

TOKEN_FILE = Path.home() / '.autofotello' / 'fotello_tokens.json'


def _dec(blob: str) -> str:
    raw = base64.b64decode(blob)
    return bytes(b ^ _XOR_KEY[i % len(_XOR_KEY)] for i, b in enumerate(raw)).decode()


def _save_fotello_tokens():
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_FILE, 'w') as f:
        json.dump({
            'refresh_token': _fotello_state['refresh_token'],
            'id_token': _fotello_state['id_token'],
            'access_token': _fotello_state['access_token'],
            'team_id': _fotello_state['team_id'],
        }, f)


def _load_fotello_tokens():
    if not TOKEN_FILE.exists():
        return False
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        _fotello_state.update({
            'refresh_token': data.get('refresh_token', ''),
            'id_token': data.get('id_token', ''),
            'access_token': data.get('access_token', ''),
            'team_id': data.get('team_id', ''),
        })
        return bool(_fotello_state['refresh_token'])
    except Exception:
        return False


def fotello_logout():
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
    _fotello_state.update({
        'refresh_token': '', 'id_token': '', 'access_token': '',
        'team_id': '', 'connected': False,
    })


def fotello_is_connected() -> bool:
    return _fotello_state.get('connected', False)


def fotello_get_status() -> dict:
    return {'connected': fotello_is_connected()}


def _ssl_context():
    import ssl
    # 1. Ưu tiên trust store gốc của OS (hỗ trợ CA của tường lửa/proxy doanh nghiệp)
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    # 2. Fallback: certifi (cho máy không có CA doanh nghiệp)
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

_CTX = _ssl_context()


def _retry(fn, max_retries=MAX_RETRIES):
    for attempt in range(max_retries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code in (429, 500, 502, 503) and attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    return fn()


def _refresh_firebase_token(refresh_token: str) -> dict:
    payload = f'grant_type=refresh_token&refresh_token={urllib.parse.quote(refresh_token)}'
    req = urllib.request.Request(
        FIREBASE_AUTH_URL,
        data=payload.encode(),
        method='POST',
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    with urllib.request.urlopen(req, context=_CTX) as resp:
        data = json.loads(resp.read())
    return {
        'id_token': data.get('id_token') or data.get('idToken', ''),
        'access_token': data.get('access_token', ''),
        'refresh_token': data.get('refresh_token', refresh_token),
        'expires_in': data.get('expires_in', '3600'),
    }


def _decode_jwt_payload(id_token: str) -> dict:
    parts = id_token.split('.')
    if len(parts) < 2:
        return {}
    try:
        return json.loads(base64.urlsafe_b64decode(parts[1] + '=='))
    except Exception:
        return {}


def _detect_team_id(id_token: str, access_token: str) -> str:
    claims = _decode_jwt_payload(id_token)
    teams_dict = claims.get('teams')
    if isinstance(teams_dict, dict) and teams_dict:
        return next(iter(teams_dict))
    for key in TEAM_ID_CLAIM_KEYS:
        if claims.get(key):
            return str(claims[key])
    uid = claims.get('user_id') or claims.get('sub', '')
    for path_tpl in TEAM_ID_USER_DOC_PATHS:
        try:
            doc = _firestore_get(path_tpl.replace('{uid}', uid), access_token)
            fields = doc.get('fields', {})
            for key in TEAM_ID_CLAIM_KEYS:
                sv = fields.get(key, {}).get(_FLD_SV)
                if sv:
                    return sv
        except Exception:
            continue
    return ''


def _fs_request(method: str, url: str, access_token: str, body=None) -> dict:
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, context=_CTX) as resp:
        return json.loads(resp.read())


def _firestore_get(doc_path: str, access_token: str) -> dict:
    url = f'{FIRESTORE_URL}/{doc_path}'
    return _retry(lambda: _fs_request('GET', url, access_token))


def _firestore_patch(doc_path: str, fields: dict, access_token: str, mask: list, log=None) -> dict:
    params = '&'.join(f'updateMask.fieldPaths={f}' for f in mask)
    url = f'{FIRESTORE_URL}/{doc_path}?{params}'
    return _retry(lambda: _fs_request('PATCH', url, access_token, {'fields': fields}))


def _firestore_run_query(access_token: str, structured_query: dict, log=None) -> list:
    url = f'{FIRESTORE_URL}:runQuery'
    result = _retry(lambda: _fs_request('POST', url, access_token, {'structuredQuery': structured_query}))
    if isinstance(result, list):
        return result
    return result.get('documents') or []


def _storage_download(gs_uri: str, access_token: str) -> bytes:
    path = gs_uri.replace('gs://', '', 1)
    bucket, _, obj = path.partition('/')
    url = f'https://firebasestorage.googleapis.com/v0/b/{bucket}/o/{urllib.parse.quote(obj, safe="")}?alt=media'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {access_token}'})
    def _do():
        with urllib.request.urlopen(req, context=_CTX) as resp:
            return resp.read()
    return _retry(_do)


def _api_post(endpoint: str, body: dict, id_token: str) -> dict:
    url = f'{FOTELLO_API}/{endpoint}'
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method='POST',
        headers={
            'Content-Type': 'application/json',
            'Authorization': id_token,
            'Origin': 'https://app.fotello.co',
            'Referer': 'https://app.fotello.co/',
        },
    )
    def _do():
        with urllib.request.urlopen(req, context=_CTX) as resp:
            return json.loads(resp.read())
    return _retry(_do)


def _get_content_type(filepath: Path) -> str:
    return CONTENT_TYPES.get(filepath.suffix.lower(), 'application/octet-stream')


def _upload_to_fotello_storage(upload_id: str, filepath: Path, id_token: str) -> None:
    content_type = _get_content_type(filepath)
    file_size = filepath.stat().st_size
    obj_name = urllib.parse.quote(f'{upload_id}/{filepath.name}', safe='')
    init_url = f'https://firebasestorage.googleapis.com/v0/b/fotello-uploads/o?name={obj_name}'

    init_req = urllib.request.Request(
        init_url,
        data=json.dumps({'contentType': content_type}).encode(),
        method='POST',
        headers={
            'Authorization': f'Firebase {id_token}',
            'X-Goog-Upload-Protocol': 'resumable',
            'X-Goog-Upload-Command': 'start',
            'X-Goog-Upload-Header-Content-Length': str(file_size),
            'X-Goog-Upload-Header-Content-Type': content_type,
            'Content-Type': 'application/json',
        },
    )
    with urllib.request.urlopen(init_req, context=_CTX) as resp:
        upload_url = resp.headers.get('X-Goog-Upload-URL') or resp.headers.get('Location', '')

    with open(filepath, 'rb') as f:
        file_data = f.read()
    upload_req = urllib.request.Request(
        upload_url,
        data=file_data,
        method='POST',
        headers={
            'Content-Type': content_type,
            'X-Goog-Upload-Command': 'upload, finalize',
            'X-Goog-Upload-Offset': '0',
        },
    )
    with urllib.request.urlopen(upload_req, context=_CTX) as resp:
        resp.read()


def _natural_key(path):
    s = str(path)
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', s)]


def _resolve_input_images(input_source) -> list[Path]:
    if isinstance(input_source, (list, tuple)):
        return sorted([Path(p) for p in input_source], key=_natural_key)
    p = Path(input_source)
    if p.is_dir():
        return sorted(
            [f for f in p.iterdir() if f.suffix.lower() in IMAGE_EXTENSIONS],
            key=_natural_key,
        )
    return [p]


def fotello_analyze_brackets(input_dir, bracket_size: int = 3, log=None, grouping_mode: str = 'time') -> dict:
    images = _resolve_input_images(input_dir)
    if not images:
        return {'ok': False, 'brackets': [], 'msg': 'Không tìm thấy ảnh'}

    def _open_any(path: Path) -> Image.Image:
        if path.suffix.lower() in {'.cr2', '.cr3', '.nef', '.arw', '.dng', '.raf'}:
            with rawpy.imread(str(path)) as raw:
                return Image.fromarray(raw.postprocess())
        return Image.open(path)

    def _get_exif_time(path: Path):
        try:
            img = Image.open(path)
            exif = img._getexif() or {}
            dt_str = exif.get(36867) or exif.get(306) or ''
            if dt_str:
                import datetime
                t = datetime.datetime.strptime(dt_str.strip(), '%Y:%m:%d %H:%M:%S')
                return t.timestamp()
        except Exception:
            pass
        try:
            return path.stat().st_mtime
        except Exception:
            return None

    def _scene_metrics(imgs):
        result = []
        for p in imgs:
            t = _get_exif_time(p)
            ph = None
            try:
                img = _open_any(p)
                ph = imagehash.phash(img)
            except Exception:
                pass
            result.append({'path': str(p), 'time': t, 'phash': ph})
        return result

    if grouping_mode == 'order_fixed':
        brackets = [
            [str(images[i]) for i in range(start, min(start + bracket_size, len(images)))]
            for start in range(0, len(images), bracket_size)
        ]
    elif grouping_mode == 'filename':
        # Gộp theo tên file: cùng scene -> bỏ đuôi phơi sáng (E01/E02/EV0/-1.0...) ra cùng 1 bracket.
        import re
        # Bỏ phần đuôi kiểu: _-_E02 | _E02 | -E02 | _EV0 | _-2.0 | (1) ở cuối tên (trước phần mở rộng)
        exp_re = re.compile(r'[ _\-]*(?:E[Vv]?[-+]?\d+(?:\.\d+)?|[-+]?\d+\.\d+EV?|\(\d+\))$', re.IGNORECASE)
        groups = {}
        order = []
        for p in images:
            stem = Path(p).stem
            key = exp_re.sub('', stem).rstrip(' _-')
            if not key:
                key = stem  # phòng tên toàn là token
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(str(p))
        brackets = [groups[k] for k in order]
    else:
        metrics = _scene_metrics(images)
        brackets = []
        used = set()
        for i, m in enumerate(metrics):
            if i in used:
                continue
            group = [m['path']]
            used.add(i)
            for j in range(i + 1, len(metrics)):
                if j in used or len(group) >= bracket_size:
                    break
                n = metrics[j]
                time_close = (m['time'] is not None and n['time'] is not None
                              and abs(m['time'] - n['time']) <= 5)
                hash_close = (m['phash'] is not None and n['phash'] is not None
                              and (m['phash'] - n['phash']) <= 10)
                if time_close or hash_close:
                    group.append(n['path'])
                    used.add(j)
            brackets.append(group)

    if log:
        log(f'Phân tích xong: {len(brackets)} bracket từ {len(images)} ảnh')
    return {'ok': True, 'brackets': brackets}


def fotello_validate_session(state=None, log=None) -> bool:
    # state=None -> dùng global (desktop). Web truyền state riêng của từng user.
    use_global = state is None
    if state is None:
        state = _fotello_state
    rt = state.get('refresh_token', '')
    if not rt:
        return False
    try:
        tokens = _refresh_firebase_token(rt)
        state.update({
            'id_token': tokens['id_token'],
            'access_token': tokens['access_token'],
            'refresh_token': tokens['refresh_token'],
            'connected': True,
        })
        if use_global:
            _save_fotello_tokens()
        return True
    except Exception as e:
        if log:
            log(f'Firebase token refresh failed: {e}')
        state['connected'] = False
        return False


def fotello_get_tokens(state=None):
    if state is None:
        state = _fotello_state
    if not fotello_validate_session(state=state):
        return None
    return (
        state['id_token'],
        state['access_token'],
        state['refresh_token'],
    )


def fotello_state_from_refresh_token(refresh_token: str, log=None):
    """Tạo state Fotello mới cho 1 user từ refresh_token (không đụng file/global).

    Dùng cho web: mỗi user có bộ token riêng. Trả về dict state hoặc None nếu token sai.
    """
    if not refresh_token:
        return None
    state = {
        'refresh_token': refresh_token.strip(),
        'id_token': '', 'access_token': '', 'team_id': '', 'connected': False,
    }
    if not fotello_validate_session(state=state, log=log):
        return None
    if not state.get('team_id'):
        try:
            state['team_id'] = _detect_team_id(state['id_token'], state['access_token'])
        except Exception:
            pass
    return state


def fotello_reconnect_saved(log=None) -> bool:
    if not _load_fotello_tokens():
        return False
    ok = fotello_validate_session(log=log)
    if ok and not _fotello_state.get('team_id'):
        try:
            team_id = _detect_team_id(_fotello_state['id_token'], _fotello_state['access_token'])
            _fotello_state['team_id'] = team_id
            _save_fotello_tokens()
        except Exception:
            pass
    return ok


def fotello_grab_tokens_from_browser(driver, log=None) -> bool:
    try:
        driver.get('https://app.fotello.co')
        time.sleep(2)
        refresh_token = driver.execute_script("return localStorage.getItem('refresh_token')")
        if not refresh_token:
            if log:
                log('Không tìm thấy refresh_token. Hãy đăng nhập vào app.fotello.co')
            return False
        _fotello_state['refresh_token'] = refresh_token
        ok = fotello_validate_session(log=log)
        if ok:
            if log:
                log('✓ Đăng nhập Fotello thành công')
            team_id = _detect_team_id(_fotello_state['id_token'], _fotello_state['access_token'])
            _fotello_state['team_id'] = team_id
            _save_fotello_tokens()
        return ok
    except Exception as e:
        if log:
            log(f'Lỗi lấy token từ browser: {e}')
        return False


def fotello_list_listings(state=None, log=None) -> list:
    if state is None:
        state = _fotello_state
    tokens = fotello_get_tokens(state=state)
    if not tokens:
        return []
    id_token, access_token, _ = tokens
    team_id = state.get('team_id', '')
    if not team_id:
        team_id = _detect_team_id(id_token, access_token)
        state['team_id'] = team_id

    try:
        docs = _firestore_run_query(access_token, {
            'from': [{'collectionId': 'listings'}],
            'where': {
                'fieldFilter': {
                    'field': {'fieldPath': 'teamId'},
                    'op': 'EQUAL',
                    'value': {_FLD_SV: team_id},
                }
            },
            'orderBy': [{'field': {'fieldPath': 'creationTime'}, 'direction': 'DESCENDING'}],
        }, log=log)
        result = []
        for doc in docs:
            doc = doc.get('document') or doc
            fields = doc.get('fields', {})
            doc_id = doc.get('name', '').split('/')[-1]
            result.append({
                'id': doc_id,
                'name': fields.get('name', {}).get(_FLD_SV, doc_id),
                'created_at': fields.get('creationTime', {}).get('timestampValue', ''),
                'enhance_count': int(fields.get('num_enhances', {}).get('integerValue', 0)),
            })
        return result
    except Exception as e:
        if log:
            log(f'Lỗi liệt kê listings: {e}')
        return []


def fotello_list_enhances_for_listing(listing_id: str, state=None, log=None) -> list:
    if state is None:
        state = _fotello_state
    tokens = fotello_get_tokens(state=state)
    if not tokens:
        return []
    _, access_token, _ = tokens
    try:
        docs = _firestore_run_query(access_token, {
            'from': [{'collectionId': _FLD_ENHANCES}],
            'where': {
                'fieldFilter': {
                    'field': {'fieldPath': 'listingId'},
                    'op': 'EQUAL',
                    'value': {_FLD_SV: listing_id},
                }
            },
        }, log=log)
        result = []
        for doc in docs:
            doc = doc.get('document') or doc
            fields = doc.get('fields', {})
            doc_id = doc.get('name', '').split('/')[-1]
            input_fnames = fields.get('inputFilenames', {}).get('arrayValue', {}).get('values', [])
            src_name = (
                input_fnames[0].get(_FLD_SV, '') if input_fnames else ''
            ) or fields.get('srcName', {}).get(_FLD_SV, '')
            result.append({
                'id': doc_id,
                'status': fields.get(_FLD_STATUS, {}).get(_FLD_SV, ''),
                'edited_image': fields.get(_FLD_EDITED, {}).get(_FLD_SV, ''),
                'edited_upsized': fields.get(_FLD_EDITED_UPSIZED, {}).get(_FLD_SV, ''),
                'is_watermarked': fields.get(_FLD_IS_WM, {}).get(_FLD_BV, True),
                'src_name': src_name,
            })
        return result
    except Exception as e:
        if log:
            log(f'Lỗi lấy enhances: {e}')
        return []


def _download_single_enhance(enhance_id, access_token, output_dir, src_name, log, fullsize=True, is_cancelled=None):
    try:
        if is_cancelled and is_cancelled():
            return None
        doc = _firestore_get(f'enhances/{enhance_id}', access_token)
        fields = doc.get('fields', {})
        gs_uri = ''
        if fullsize:
            gs_uri = (fields.get(_FLD_EDITED_UPSIZED, {}).get(_FLD_SV, '')
                      or fields.get(_FLD_EDITED, {}).get(_FLD_SV, ''))
        else:
            gs_uri = fields.get(_FLD_EDITED, {}).get(_FLD_SV, '')
        if not gs_uri:
            return None
        data = _storage_download(gs_uri, access_token)
        ext = Path(gs_uri).suffix or '.jpg'
        out_name = Path(src_name).stem + ext if src_name else f'{enhance_id}{ext}'
        out_path = output_dir / out_name
        out_path.write_bytes(data)
        if log:
            log(f'✓ {out_name}')
        return out_path
    except Exception as e:
        if log:
            log(f'✗ {enhance_id}: {e}')
        return None


def fotello_download_listing(listing_id: str, output_dir: str, state=None, log=None, is_cancelled=None) -> list:
    if state is None:
        state = _fotello_state
    tokens = fotello_get_tokens(state=state)
    if not tokens:
        if log: log('❌ Chưa đăng nhập Fotello')
        return []
    _, access_token, _ = tokens
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    enhances = fotello_list_enhances_for_listing(listing_id, state=state, log=log)
    if not enhances:
        if log: log('❌ Không có ảnh nào để tải')
        return []
    saved = []
    for en in enhances:
        if is_cancelled and is_cancelled():
            break
        try:
            _firestore_patch(
                f'enhances/{en["id"]}',
                {_FLD_IS_WM: {_FLD_BV: False}},
                access_token,
                [_FLD_IS_WM],
            )
        except Exception:
            pass
        p = _download_single_enhance(
            en['id'], access_token, out, en.get('src_name', ''), log,
            fullsize=True, is_cancelled=is_cancelled,
        )
        if p:
            saved.append(str(p))
    return saved


def fotello_batch_download(listing_ids: list, output_dir: str, log=None, progress_fn=None, is_cancelled=None) -> int:
    total = 0
    for i, lid in enumerate(listing_ids):
        if is_cancelled and is_cancelled():
            break
        paths = fotello_download_listing(lid, output_dir, log=log, is_cancelled=is_cancelled)
        total += len(paths)
        if progress_fn:
            progress_fn(i + 1, len(listing_ids))
    return total


def fotello_upload_and_enhance(
    input_dir,
    output_dir: str,
    log=None,
    progress_fn=None,
    status_fn=None,
    is_cancelled=None,
    preferences=None,
    predefined_brackets=None,
    state=None,
) -> dict:
    if state is None:
        state = _fotello_state
    tokens = fotello_get_tokens(state=state)
    if not tokens:
        return {'ok': False, 'msg': 'Chưa đăng nhập Fotello'}
    id_token, access_token, _ = tokens
    team_id = state.get('team_id', '')
    if not team_id:
        team_id = _detect_team_id(id_token, access_token)
        state['team_id'] = team_id

    preferences = preferences or {}
    is_cancelled = is_cancelled or (lambda: False)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    images = _resolve_input_images(input_dir)
    if not images:
        return {'ok': False, 'msg': 'Không tìm thấy ảnh'}

    if log: log(f'Bắt đầu xử lý {len(images)} ảnh với Fotello…')

    if predefined_brackets:
        brackets = predefined_brackets
    else:
        br_result = fotello_analyze_brackets(
            input_dir,
            bracket_size=preferences.get('bracket_size', 3),
            log=log,
            grouping_mode=preferences.get('grouping_mode', 'time'),
        )
        brackets = br_result.get('brackets', [[str(p)] for p in images])

    total_steps = len(images) + len(brackets) + 1
    step = 0

    def _upload_one(filepath: Path):
        if is_cancelled():
            return filepath.name, None
        try:
            up_resp = _api_post(_EP_CREATE_UPLOAD, {
                'filename': filepath.name,
                'teamId': team_id,
            }, id_token)
            upload_id = up_resp.get('id', '')
            if not upload_id:
                raise ValueError(f'create-upload trả về không có id: {up_resp}')
            _upload_to_fotello_storage(upload_id, filepath, id_token)
            if log: log(f'✓ {filepath.name}')
            return filepath.name, upload_id
        except Exception as e:
            if log: log(f'✗ {filepath.name}: {e}')
            return filepath.name, None

    upload_id_map: dict = {}
    max_workers = min(6, len(images))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_upload_one, fp): fp for fp in images}
        for future in as_completed(futures):
            name, uid = future.result()
            if uid:
                upload_id_map[name] = uid
            step += 1
            if progress_fn: progress_fn(step, total_steps)

    if not upload_id_map:
        return {'ok': False, 'msg': 'Upload thất bại toàn bộ'}

    listing_name = preferences.get('project_name') or preferences.get('name') or Path(str(input_dir)).name or 'AutoFotello Upload'
    if log: log(f'Tạo listing "{listing_name}"…')
    try:
        listing_resp = _api_post(_EP_CREATE_LISTING, {
            'name': listing_name,
            'num_total_brackets': len(brackets),
            'filenames': [f.name for f in images],
            'isDemoListing': False,
            'teamId': team_id,
        }, id_token)
        listing_id = listing_resp.get('id') or listing_resp.get('listingId', '')
    except Exception as e:
        return {'ok': False, 'msg': f'Lỗi tạo listing: {e}'}

    if log: log('Gửi yêu cầu enhance…')
    enhance_ids = []
    sky = preferences.get('sky_replacement', True)
    persp = preferences.get('perspective_correction', True)
    for bracket in brackets:
        bracket_ids = [upload_id_map[Path(p).name] for p in bracket if Path(p).name in upload_id_map]
        if not bracket_ids:
            continue
        try:
            resp = _api_post(_EP_CREATE_ENHANCE, {
                'upload_ids': bracket_ids,
                'listing_id': listing_id,
                'preferences': {
                    'contrast_style': preferences.get('contrast_style', 'signature'),
                    'exterior_sky_replacement': 'on' if sky else 'off',
                    'perspective_correction': 'on' if persp else 'off',
                    'custom_style_id': preferences.get('custom_style_id', None),
                    'cloud_style': preferences.get('cloud_style', 'original'),
                },
                'teamId': team_id,
            }, id_token)
            eid = resp.get('id') or resp.get('enhanceId')
            if eid:
                enhance_ids.append(eid)
                try:
                    _firestore_patch(
                        f'enhances/{eid}',
                        {_FLD_IS_WM: {_FLD_BV: False}},
                        access_token,
                        [_FLD_IS_WM],
                    )
                except Exception:
                    pass
        except Exception as e:
            if log: log(f'Lỗi tạo enhance: {e}')
        step += 1
        if progress_fn: progress_fn(step, total_steps)

    msg = f'Đã upload thành công và gửi yêu cầu xử lý ({len(enhance_ids)} brackets)!'
    if log: log(f'✓ {msg}')
    return {'ok': True, 'msg': msg, 'listing_id': listing_id}
