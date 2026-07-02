"""
engines/autohdr_engine.py — AutoHDR Integration Engine
Copy & adapt từ AutoFotello_source/autohdr_engine.py
Thay JS callbacks → log_callback(msg) / progress_callback(current, total)
"""
import re
import os
import json
import time
import logging
import sqlite3
import shutil
from pathlib import Path
from concurrent.futures import as_completed, ThreadPoolExecutor
from urllib.parse import urlparse, unquote

import requests

logger = logging.getLogger(__name__)

_AUTOHDR_BASE         = 'https://www.autohdr.com'
_BATCH                = 10
_MAX_DOWNLOAD_WORKERS = 8
_UPSCALE_POLL_INTERVAL = 3
_UPSCALE_TIMEOUT      = 300

_MODEL_IDS = {
    'classic':          1,
    'classic_v4':       16,
    'lisa':             3,
    'twilight_golden':  6,
    'twilight_pink':    7,
    'twilight_midnight': 8,
}


def _parse_cookie_jar(cookie_str: str) -> dict:
    jar = {}
    for part in cookie_str.split(';'):
        part = part.strip()
        if '=' in part:
            k, v = part.split('=', 1)
            jar[k.strip()] = v.strip()
    return jar


def _is_clean_photo_url(url: str) -> bool:
    # Link chứa '/watermarked/' là bản có watermark -> KHÔNG coi là sạch.
    return bool(url) and url.startswith('http') and '/watermarked/' not in url


def _finalize_clean_url(session: requests.Session, photo: dict, cookie: str):
    """Gọi finalize-adjustment để lấy link ảnh SẠCH (không watermark) từ ảnh watermark."""
    pid = photo.get('id')
    url = photo.get('photo_url') or photo.get('display_url') or photo.get('url') or ''
    if not pid or not url:
        return None
    # s3_key = phần path sau '/watermarked/' (vd: <uuid>/processed/DSC_1029.jpg)
    path = unquote(urlparse(url).path).lstrip('/')
    if path.startswith('watermarked/'):
        path = path[len('watermarked/'):]
    if not path:
        return None
    try:
        data = _api_post(session, '/api/proxy/photos/finalize-adjustment', {
            'photo_id': str(pid),
            'add_clouds': False,
            's3_key': path,
            'preserve_photo': True,
        }, cookie)
        if data.get('success') and data.get('url'):
            return data['url']
    except Exception as e:
        logger.debug('_finalize_clean_url %s: %s', pid, e)
    return None


def _normalize_photo_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    return name.strip()


def _photo_filename(photo: dict) -> str:
    name = photo.get('filename') or photo.get('name') or ''
    if not name:
        url = photo.get('photo_url') or photo.get('url') or ''
        if url:
            path = urlparse(url).path
            name = unquote(path.split('/')[-1])
    if not name:
        name = f"{photo.get('id', 0)}.jpg"
    return _normalize_photo_filename(name)


def _safe_folder_name(name: str) -> str:
    return re.sub(r'[^\w\s-]', '', name).strip().replace(' ', '_')


def _find_value(data, key: str):
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for v in data.values():
            r = _find_value(v, key)
            if r is not None:
                return r
    elif isinstance(data, list):
        for item in data:
            r = _find_value(item, key)
            if r is not None:
                return r
    return None


def _extract_guid(text: str):
    m = re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', text, re.I)
    return m.group(0) if m else None


def _cookie_header_from_jar(cookie_jar) -> str:
    if hasattr(cookie_jar, 'items'):
        return '; '.join(f'{k}={v}' for k, v in cookie_jar.items())
    return '; '.join(f'{c.name}={c.value}' for c in cookie_jar)


def _browser_cookie_sources() -> list:
    sources = []
    home = Path.home()
    chrome_base = home / 'AppData/Local/Google/Chrome/User Data'
    for profile in ['Default', 'Profile 1', 'Profile 2']:
        cookie_db = chrome_base / profile / 'Network' / 'Cookies'
        if not cookie_db.exists():
            cookie_db = chrome_base / profile / 'Cookies'
        if cookie_db.exists():
            sources.append(('chrome', cookie_db, chrome_base / profile))
    edge_base = home / 'AppData/Local/Microsoft/Edge/User Data'
    for profile in ['Default', 'Profile 1']:
        cookie_db = edge_base / profile / 'Network' / 'Cookies'
        if not cookie_db.exists():
            cookie_db = edge_base / profile / 'Cookies'
        if cookie_db.exists():
            sources.append(('edge', cookie_db, edge_base / profile))
    return sources


def _load_autohdr_cookie_from_browser() -> tuple:
    for browser_name, cookie_db, profile_path in _browser_cookie_sources():
        tmp = Path(os.environ.get('TEMP', '/tmp')) / f'_autohdr_cookies_{browser_name}.db'
        try:
            shutil.copy2(cookie_db, tmp)
            conn = sqlite3.connect(str(tmp))
            try:
                cur = conn.execute(
                    "SELECT name, value FROM cookies WHERE host_key LIKE '%autohdr.com%'"
                )
                rows = cur.fetchall()
            except Exception:
                cur = conn.execute(
                    "SELECT name, encrypted_value FROM cookies WHERE host_key LIKE '%autohdr.com%'"
                )
                rows = [(r[0], r[1].decode('utf-8', 'ignore') if isinstance(r[1], bytes) else r[1]) for r in cur.fetchall()]
            conn.close()
            if rows:
                cookie_str = '; '.join(f'{name}={val}' for name, val in rows if val)
                if cookie_str:
                    return cookie_str, browser_name
        except Exception as e:
            logger.debug('Cookie extract failed for %s: %s', browser_name, e)
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass
    return '', ''


def _api_get(session: requests.Session, endpoint: str, cookie: str) -> dict:
    resp = session.get(
        f'{_AUTOHDR_BASE}{endpoint}',
        headers={'Cookie': cookie, 'User-Agent': 'Mozilla/5.0'},
    )
    resp.raise_for_status()
    return resp.json()


def _api_post(session: requests.Session, endpoint: str, body: dict, cookie: str) -> dict:
    resp = session.post(
        f'{_AUTOHDR_BASE}{endpoint}',
        json=body,
        headers={'Cookie': cookie, 'User-Agent': 'Mozilla/5.0'},
    )
    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:300]
        raise requests.HTTPError(
            f'{resp.status_code} {resp.reason} — {endpoint} — body: {detail}',
            response=resp,
        )
    return resp.json()


def _validate_autohdr_cookie(cookie: str) -> tuple:
    try:
        resp = requests.get(
            f'{_AUTOHDR_BASE}/api/auth/get-session',
            headers={'Cookie': cookie, 'User-Agent': 'Mozilla/5.0'},
            timeout=10,
            verify=False,
        )
        if resp.status_code == 200:
            data = resp.json()
            user = data.get('user') or {}
            uid = str(user.get('id', ''))
            if uid:
                return True, uid
            return False, 'no_user'
        return False, f'HTTP {resp.status_code}'
    except Exception as e:
        return False, str(e)


def _find_own_photoshoot(sess: requests.Session, guid: str, cookie: str):
    try:
        data = _api_get(sess, f'/api/proxy/photoshoots/uuid/{guid}/processed_photos?page=1&page_size=1', cookie)
        if data:
            return data
    except Exception:
        pass
    try:
        sess_data = _api_get(sess, '/api/auth/get-session', cookie)
        user = sess_data.get('user') or {}
        user_id = user.get('id')
        if not user_id:
            return None
        ps_list = _api_get(sess, f'/api/users/{user_id}/photoshoots?limit=100&offset=0', cookie)
        photoshoots = ps_list if isinstance(ps_list, list) else (ps_list.get('photoshoots') or [])
        return _find_by_uuid(sess, guid, cookie) or (photoshoots[0] if photoshoots else None)
    except Exception as e:
        logger.error('_find_own_photoshoot error: %s', e)
        return None


def _find_by_uuid(sess: requests.Session, guid: str, cookie: str):
    try:
        data = _api_get(sess, f'/api/proxy/photoshoots/uuid/{guid}/processed_photos?page=1&page_size=1', cookie)
        return data if data else None
    except Exception:
        return None


def _fetch_all_photos(sess: requests.Session, guid: str, uuid_fallback: bool, ps_id: int, cookie: str) -> list:
    try:
        all_photos = []
        page = 1
        page_size = 100
        while True:
            data = _api_get(
                sess,
                f'/api/proxy/photoshoots/{ps_id}/processed_photos?page={page}&page_size={page_size}',
                cookie,
            )
            if isinstance(data, list):
                batch = data
            else:
                batch = data.get('processed_photos') or data.get('photos') or data.get('results') or []
            if not batch:
                break
            all_photos.extend(batch)
            if len(batch) < page_size:   # hết trang
                break
            page += 1
        return all_photos
    except Exception as e:
        logger.error('_fetch_all_photos error: %s', e)
        return []


def _photoshoot_folder_name(ps: dict, photos: list, guid: str) -> str:
    name = ps.get('name') or ps.get('address') or ps.get('title') or guid or 'autohdr_shoot'
    return _safe_folder_name(name)


def _trigger_upscale(sess: requests.Session, photo_ids: list, cookie: str) -> bool:
    try:
        _api_post(sess, '/api/upscale-for-print', {'photo_ids': photo_ids}, cookie)
        return True
    except Exception as e:
        logger.error('_trigger_upscale error: %s', e)
        return False


def _poll_upscale_urls(sess: requests.Session, photo_ids: list, cookie: str) -> dict:
    results: dict = {}
    remaining = set(photo_ids)
    deadline = time.time() + _UPSCALE_TIMEOUT

    while remaining and time.time() < deadline:
        try:
            for photo_id in list(remaining):
                try:
                    data = sess.get(
                        f'{_AUTOHDR_BASE}/api/proxy/photos/{photo_id}',
                        headers={'Cookie': cookie},
                    ).json()
                    url = data.get('upscaled_url') or data.get('processed_url')
                    if url and _is_clean_photo_url(url):
                        results[photo_id] = url
                        remaining.discard(photo_id)
                except Exception:
                    pass
        except Exception:
            pass
        if remaining:
            time.sleep(_UPSCALE_POLL_INTERVAL)

    return results


def _poll_processed_photos(sess: requests.Session, ps_uuid: str, cookie: str,
                            expected: int, stop_event, log_fn, timeout: int = 1800) -> list:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            break
        try:
            data = _api_get(
                sess,
                f'/api/proxy/photoshoots/uuid/{ps_uuid}/processed_photos?page=1&page_size=100',
                cookie,
            )
            photos = data.get('processed_photos') or []
            ready = [p for p in photos if p.get('photo_url')]
            log_fn(f'Chờ xử lý: {len(ready)}/{expected} ảnh xong…')
            if len(ready) >= expected:
                return ready
        except Exception as e:
            logger.debug('_poll_processed_photos: %s', e)
        time.sleep(_UPSCALE_POLL_INTERVAL)
    return []


def _build_download_targets(clean_urls: dict, photo_map: dict, dest: Path) -> list:
    targets = []
    used = set()
    for photo_id, url in clean_urls.items():
        filename = _normalize_photo_filename(photo_map.get(photo_id) or f'{photo_id}.jpg')
        # chống trùng tên -> thêm hậu tố _2, _3… để không đè mất ảnh
        if filename.lower() in used:
            stem, ext = os.path.splitext(filename)
            n = 2
            while f'{stem}_{n}{ext}'.lower() in used:
                n += 1
            filename = f'{stem}_{n}{ext}'
        used.add(filename.lower())
        targets.append((photo_id, url, dest / filename, filename))
    return targets


def _download_one(target: tuple, stop_event, retries: int = 3) -> tuple:
    photo_id, url, dest_path, filename = target
    last_err = ''
    for attempt in range(1, retries + 1):
        if stop_event and stop_event.is_set():
            return False, filename, 'cancelled'
        try:
            resp = requests.get(url, timeout=120, stream=True, verify=False)
            resp.raise_for_status()
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, 'wb') as f:
                for chunk in resp.iter_content(65536):
                    f.write(chunk)
            # kiểm tra file không rỗng
            if dest_path.stat().st_size == 0:
                raise IOError('file rỗng')
            return True, filename, ''
        except Exception as e:
            last_err = str(e)
            # xóa file dở để không bị coi là đã tải
            try:
                if dest_path.exists():
                    dest_path.unlink()
            except Exception:
                pass
            if attempt < retries:
                time.sleep(1.5 * attempt)   # chờ tăng dần rồi thử lại
    return False, filename, f'{last_err} (đã thử {retries} lần)'


def run_clean_download(cookie: str, photoshoot_url: str, output_dir, log_fn, progress_fn, stop_event, create_subfolder: bool = True) -> int:
    output_dir = Path(output_dir)
    sess = requests.Session()
    sess.headers.update({'Cookie': cookie, 'User-Agent': 'Mozilla/5.0'})

    _id_str = str(photoshoot_url).strip()
    if _id_str.isdigit():
        ps_id = int(_id_str)
        guid = _id_str
        ps_name = f'autohdr_{ps_id}'
        log_fn(f'Đang lấy photoshoot #{ps_id}…')
    else:
        guid = _extract_guid(photoshoot_url) or photoshoot_url.rstrip('/').split('/')[-1]
        log_fn(f'Đang tìm photoshoot {guid}…')
        ps = _find_own_photoshoot(sess, guid, cookie)
        if not ps:
            log_fn('❌ Không tìm thấy photoshoot')
            return 0
        ps_id = ps.get('id') or ps.get('ps_id') or 0
        ps_name = _photoshoot_folder_name(ps, [], guid)

    dest = output_dir / ps_name if create_subfolder else output_dir
    dest.mkdir(parents=True, exist_ok=True)

    try:
        sess.post(
            f'{_AUTOHDR_BASE}/api/proxy/photoshoots/{ps_id}/mark-downloaded',
            headers={'Cookie': cookie, 'Content-Length': '0'},
            timeout=15, verify=False,
        )
    except Exception:
        pass

    log_fn('Đang lấy danh sách ảnh…')
    photos = _fetch_all_photos(sess, guid, True, ps_id, cookie)
    if not photos:
        log_fn('❌ Không có ảnh trong photoshoot')
        return 0

    clean_urls: dict = {}
    photo_map: dict = {}
    need_upscale = []

    to_finalize = []
    for p in photos:
        pid = p.get('id')
        photo_map[pid] = _photo_filename(p)
        # Ưu tiên link đã sạch sẵn (upscaled/processed, không watermark)
        url = p.get('upscaled_url') or p.get('processed_url') or ''
        if _is_clean_photo_url(url):
            clean_urls[pid] = url
        else:
            to_finalize.append(p)

    # Lấy link ảnh sạch (finalize-adjustment) song song cho các ảnh còn watermark
    if to_finalize:
        log_fn(f'Đang lấy link ảnh cho {len(to_finalize)} ảnh…')
        with ThreadPoolExecutor(max_workers=_MAX_DOWNLOAD_WORKERS) as ex:
            futs = {ex.submit(_finalize_clean_url, sess, p, cookie): p for p in to_finalize}
            for fut in as_completed(futs):
                if stop_event and stop_event.is_set():
                    break
                pid = futs[fut].get('id')
                try:
                    clean = fut.result()
                except Exception:
                    clean = None
                if clean:
                    clean_urls[pid] = clean
                else:
                    need_upscale.append(pid)

    # ── TẠM TẮT: bước upscale AutoHDR hiện chưa dùng được (API không khả dụng) ──
    # if need_upscale:
    #     log_fn(f'Kích hoạt upscale cho {len(need_upscale)} ảnh…')
    #     for i in range(0, len(need_upscale), _BATCH):
    #         batch = need_upscale[i:i + _BATCH]
    #         _trigger_upscale(sess, batch, cookie)
    #     log_fn('Đang đợi upscale hoàn thành…')
    #     upscaled = _poll_upscale_urls(sess, need_upscale, cookie)
    #     clean_urls.update(upscaled)
    if need_upscale:
        log_fn(f'⏭ Bỏ qua upscale cho {len(need_upscale)} ảnh (chức năng upscale tạm tắt).')

    targets = _build_download_targets(clean_urls, photo_map, dest)
    if not targets:
        log_fn('❌ Không có ảnh nào để tải')
        return 0

    total_photos = len(photos)
    log_fn(f'Tải {len(targets)} ảnh…')
    downloaded = 0
    failed = []   # ảnh tải lỗi sau 3 lần thử
    with ThreadPoolExecutor(max_workers=_MAX_DOWNLOAD_WORKERS) as ex:
        futs = {ex.submit(_download_one, t, stop_event): t for t in targets}
        for i, fut in enumerate(as_completed(futs), 1):
            ok, fname, err = fut.result()
            if ok:
                downloaded += 1
                log_fn(f'✓ {fname}')
            elif err != 'cancelled':
                failed.append(fname)
                log_fn(f'✗ {fname}: {err}')
            progress_fn(i, len(targets))
            if stop_event and stop_event.is_set():
                break

    # Ảnh không lấy được link sạch (finalize + upscale đều thất bại)
    no_link = [photo_map[pid] for pid in photo_map if pid not in clean_urls]

    missing = failed + no_link
    if missing:
        log_fn(f'⚠️ THIẾU {len(missing)}/{total_photos} ảnh — vui lòng tải lại các ảnh sau:')
        for n in failed:
            log_fn(f'   ✗ (tải lỗi) {n}')
        for n in no_link:
            log_fn(f'   ✗ (không lấy được link) {n}')
    else:
        log_fn(f'✅ Đã tải đủ {downloaded}/{total_photos} ảnh')

    return downloaded


def autohdr_list_listings(cookie, log_fn) -> list:
    sess = requests.Session()
    sess.headers.update({'Cookie': cookie, 'User-Agent': 'Mozilla/5.0'})

    try:
        sess_data = _api_get(sess, '/api/auth/get-session', cookie)
        user = sess_data.get('user') or {}
        user_id = user.get('id')
        if not user_id:
            log_fn('❌ Không lấy được thông tin user')
            return []

        data = _api_get(sess, f'/api/users/{user_id}/photoshoots?limit=50&offset=0', cookie)
        photoshoots = data if isinstance(data, list) else (data.get('photoshoots') or [])

        result = []
        for ps in photoshoots:
            result.append({
                'id': ps.get('id'),
                'uuid': ps.get('name') or ps.get('unique_str') or '',
                'name': ps.get('address') or ps.get('title') or '',
                'photo_count': 0,
                'created_at': ps.get('created_at') or ps.get('creation_date_utc') or '',
            })

        # Tải số lượng ảnh thực tế của từng photoshoot song song để tránh hiển thị "0 ảnh"
        def _fetch_count(ps_id) -> int:
            try:
                resp = sess.get(
                    f'{_AUTOHDR_BASE}/api/proxy/photoshoots/{ps_id}/processed_photos?page=1&page_size=100',
                    headers={'Cookie': cookie, 'User-Agent': 'Mozilla/5.0'},
                    timeout=10, verify=False
                )
                if resp.status_code == 200:
                    res_data = resp.json()
                    if isinstance(res_data, list):
                        return len(res_data)
                    elif isinstance(res_data, dict):
                        return len(res_data.get('processed_photos') or [])
            except Exception:
                pass
            return 0

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(_fetch_count, item['id']): item for item in result if item['id']}
            for fut in as_completed(futures):
                item = futures[fut]
                item['photo_count'] = fut.result()

        return result
    except Exception as e:
        log_fn(f'❌ Lỗi liệt kê listings: {e}')
        return []


def autohdr_upload_and_enhance(cookie, input_dir, savedir, address, log_fn, progress_fn, stop_event, options=None) -> dict:
    options = options or {}
    image_exts = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.cr2', '.nef', '.arw', '.dng', '.webp', '.bmp'}
    _UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'

    if isinstance(input_dir, (list, tuple)):
        files = [Path(f) for f in input_dir if Path(f).suffix.lower() in image_exts]
    else:
        input_path = Path(input_dir)
        if input_path.is_dir():
            files = sorted(f for f in input_path.iterdir() if f.suffix.lower() in image_exts)
        else:
            files = [input_path] if input_path.is_file() else []

    if not files:
        return {'ok': False, 'msg': 'Không tìm thấy ảnh', 'count': 0}

    sess = requests.Session()
    sess.headers.update({'User-Agent': _UA, 'Accept': 'application/json', 'Cookie': cookie})

    try:
        log_fn('Đang lấy thông tin tài khoản…')
        sess_data = _api_get(sess, '/api/auth/get-session', cookie)
        user = sess_data.get('user') or {}
        email = user.get('email', '')
        uid = user.get('id', '')
        if not user.get('id'):
            return {'ok': False, 'msg': 'Không xác thực được tài khoản AutoHDR', 'count': 0}

        import uuid as _uuid_mod
        unique_str = str(_uuid_mod.uuid4())
        fnames = [f.name for f in files]
        total_steps = len(files) + 3

        log_fn(f'Đang lấy presigned URL cho {len(files)} ảnh…')
        pre_resp = _api_post(sess, '/api/proxy/generate_presigned_urls', {
            'unique_str': unique_str,
            'files': [{'filename': fname, 'md5': ''} for fname in fnames],
        }, cookie)
        url_map = {item['filename']: item['url'] for item in pre_resp.get('presignedUrls', [])}
        log_fn(f'✓ Nhận {len(url_map)} presigned URL')

        log_fn(f'Đang upload {len(files)} ảnh lên S3…')
        ok_s3 = 0

        def _upload_s3(filepath: Path):
            if stop_event and stop_event.is_set():
                return False
            s3_url = url_map.get(filepath.name)
            if not s3_url:
                log_fn(f'⚠ Bỏ qua {filepath.name} (không có presigned URL)')
                return False
            try:
                with open(filepath, 'rb') as fh:
                    raw = fh.read()
                sr = requests.put(
                    s3_url, data=raw, timeout=120,
                    headers={'Content-Type': 'application/octet-stream', 'x-amz-acl': 'private'},
                )
                if sr.status_code in (200, 204):
                    log_fn(f'✓ {filepath.name}')
                    return True
                log_fn(f'✗ {filepath.name} — S3 {sr.status_code}')
                return False
            except Exception as e:
                log_fn(f'✗ {filepath.name}: {e}')
                return False

        if stop_event and stop_event.is_set():
            return {'ok': False, 'msg': 'Đã huỷ', 'count': 0}

        max_workers = min(6, len(files))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_upload_s3, fp): fp for fp in files}
            for i, fut in enumerate(as_completed(futures), 1):
                if fut.result():
                    ok_s3 += 1
                progress_fn(i, total_steps)

        log_fn('Finalize upload…')
        _api_post(sess, '/api/proxy/finalize_upload', {'unique_str': unique_str}, cookie)
        log_fn('✓ Finalize OK')
        progress_fn(len(files) + 1, total_steps)

        model_name = options.get('model', 'classic')
        indoor_id = _MODEL_IDS.get(model_name, 3)
        log_fn(f'Kích hoạt AI [{model_name}] cho {ok_s3} ảnh…')
        cookie_jar = _parse_cookie_jar(cookie)
        ar_resp = requests.post(
            f'{_AUTOHDR_BASE}/api/inference/associate-and-run',
            headers={'Accept': 'application/json', 'Content-Type': 'application/json', 'User-Agent': _UA},
            cookies=cookie_jar,
            json={
                'unique_str': unique_str,
                'email': email,
                'firstname': '',
                'lastname': '',
                'address': address,
                'spoofId': uid,
                'smartlook_url': None,
                'indoor_model_id': indoor_id,
                'outdoor_model_id': None,
                'files_count': ok_s3,
                'grass_replacement': options.get('grass_replacement', False),
                'perspective_correction': options.get('perspective_correction', True),
                'special_attention': False,
                'declutter': options.get('declutter', False),
                'photoshoot_id': None,
            },
            verify=False, timeout=60,
        )
        ar_resp.raise_for_status()
        ar_data = ar_resp.json()
        ps_uuid = ar_data.get('uuid') or ar_data.get('photoshoot_uuid') or unique_str
        log_fn('✓ AI đang xử lý ảnh!')
        progress_fn(len(files) + 2, total_steps)

        msg = f'Đã upload {ok_s3} ảnh và kích hoạt xử lý AI thành công!'
        log_fn(msg)
        return {'ok': True, 'msg': msg, 'count': ok_s3, 'ps_uuid': ps_uuid}

    except Exception as e:
        logger.exception('autohdr_upload_and_enhance failed')
        return {'ok': False, 'msg': str(e), 'count': 0}
