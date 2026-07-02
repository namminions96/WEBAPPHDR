"""
web/auth_web.py — Đăng nhập Supabase cho bản web ĐA NGƯỜI DÙNG (bỏ khóa HWID).

Tái dùng client + tiện ích từ core.supabase_auth, nhưng:
- KHÔNG kiểm tra/ghi HWID (nhiều user chung 1 server).
- KHÔNG ghi file session chung; phiên do Flask session (cookie) giữ theo từng trình duyệt.
"""
import logging
from datetime import datetime, timezone

import core.supabase_auth as supabase_auth
from core.config import PROFILES_TABLE, ACCESS_RPC, SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger(__name__)

# Client riêng cho OAuth (PKCE) — cần flow_type='pkce' để đổi code lấy session.
_oauth_client = None


def _get_oauth_client():
    global _oauth_client
    if _oauth_client is not None:
        return _oauth_client
    try:
        from supabase import create_client
        # create_client cần SyncClientOptions (có 'storage'); ClientOptions cơ bản sẽ lỗi.
        try:
            from supabase.lib.client_options import SyncClientOptions as _Opts
        except Exception:
            from supabase.lib.client_options import ClientOptions as _Opts
        _oauth_client = create_client(
            SUPABASE_URL, SUPABASE_KEY,
            _Opts(flow_type='pkce', auto_refresh_token=False),
        )
    except Exception as e:
        logger.error(f'Không tạo được OAuth client: {e}')
        _oauth_client = None
    return _oauth_client


def _read_access(client, user) -> dict:
    """Đọc role/expire (bỏ HWID). Trả {ok, role, expire_at} hoặc {ok:False,msg}."""
    role = 'member'
    expire_at = None
    has_profile = False
    try:
        access = client.rpc(ACCESS_RPC).execute().data
    except Exception as e:
        logger.warning(f'check_access RPC lỗi, fallback profiles: {e}')
        access = None

    if access:
        if access.get('blocked'):
            return {'ok': False, 'msg': 'Tài khoản của bạn đã bị khóa. Vui lòng liên hệ Admin!'}
        role = access.get('role', 'member')
        expire_at = access.get('expire_at')
        has_profile = True
        if access.get('expired'):
            return {'ok': False, 'msg': 'Tài khoản của bạn đã hết hạn sử dụng!'}
    else:
        try:
            pr = client.table(PROFILES_TABLE).select('role, expire_at, blocked').eq('id', user.id).execute()
            if pr.data:
                p = pr.data[0]
                role = p.get('role', 'member')
                expire_at = p.get('expire_at')
                has_profile = True
                if p.get('blocked'):
                    return {'ok': False, 'msg': 'Tài khoản của bạn đã bị khóa. Vui lòng liên hệ Admin!'}
        except Exception as e:
            logger.warning(f'Không đọc được profiles: {e}')

    if not has_profile:
        return {'ok': False, 'msg': 'Tài khoản chưa được kích hoạt hoặc cấp phép sử dụng!'}
    if role in ('banned', 'pending', 'disabled'):
        return {'ok': False, 'msg': f'Tài khoản đang ở trạng thái "{role}". Vui lòng liên hệ Admin!'}
    if supabase_auth.is_user_expired(expire_at):
        return {'ok': False, 'msg': 'Tài khoản của bạn đã hết hạn sử dụng!'}
    return {'ok': True, 'role': role, 'expire_at': expire_at}


def google_start(redirect_to: str) -> str | None:
    """Tạo URL OAuth Google của Supabase (PKCE). Trả URL để redirect trình duyệt."""
    client = _get_oauth_client()
    if not client:
        return None
    try:
        res = client.auth.sign_in_with_oauth({
            'provider': 'google',
            'options': {'redirect_to': redirect_to},
        })
        return res.url
    except Exception as e:
        logger.error(f'google_start lỗi: {e}')
        return None


def google_finish(code: str) -> dict:
    """Đổi auth code lấy session, đọc quyền (bỏ HWID). Trả dict như web_login."""
    client = _get_oauth_client()
    if not client:
        return {'ok': False, 'msg': 'OAuth chưa cấu hình.'}
    try:
        auth_res = client.auth.exchange_code_for_session({'auth_code': code})
        session = auth_res.session
        user = auth_res.user or client.auth.get_user().user
        acc = _read_access(client, user)
        if not acc.get('ok'):
            _signout(client)
            return acc
        return {
            'ok': True, 'user_id': user.id, 'email': user.email, 'username': user.email,
            'role': acc['role'], 'expire_at': acc['expire_at'],
            'access_token': session.access_token, 'refresh_token': session.refresh_token,
        }
    except Exception as e:
        logger.error(f'google_finish lỗi: {e}')
        return {'ok': False, 'msg': f'Đăng nhập Google thất bại: {e}'}


def web_login(email: str, password: str) -> dict:
    client = supabase_auth.get_supabase_client()
    if not client:
        return {'ok': False, 'msg': 'Supabase chưa được cấu hình (SUPABASE_URL/SUPABASE_KEY).'}
    try:
        res = client.auth.sign_in_with_password({'email': email, 'password': password})
        user = res.user

        acc = _read_access(client, user)
        if not acc.get('ok'):
            _signout(client)
            return acc
        role = acc['role']
        expire_at = acc['expire_at']

        # Cập nhật last_login_at (không chặn nếu lỗi).
        try:
            client.table(PROFILES_TABLE).update(
                {'last_login_at': datetime.now(timezone.utc).isoformat()}
            ).eq('id', user.id).execute()
        except Exception:
            pass

        return {
            'ok': True,
            'user_id': user.id,
            'email': user.email,
            'username': user.email,
            'role': role,
            'expire_at': expire_at,
            'access_token': res.session.access_token,
            'refresh_token': res.session.refresh_token,
        }
    except Exception as e:
        err = str(e)
        if 'Invalid login credentials' in err or 'should be a valid email' in err:
            return {'ok': False, 'msg': 'Sai tài khoản hoặc mật khẩu!'}
        return {'ok': False, 'msg': f'Đăng nhập lỗi: {e}'}


def _rest_check_access(access_token: str) -> tuple:
    """Gọi RPC check_access qua REST bằng JWT của user (STATELESS — an toàn đa user).

    Trả (status_code, data|None). status_code=0 nếu lỗi mạng.
    """
    import requests
    try:
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/rpc/{ACCESS_RPC}',
            headers={
                'apikey': SUPABASE_KEY,
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
            },
            json={}, timeout=8,
        )
        try:
            data = r.json()
        except Exception:
            data = None
        return r.status_code, data
    except Exception as e:
        logger.warning(f'check_access REST lỗi mạng: {e}')
        return 0, None


def _rest_refresh(refresh_token: str) -> dict | None:
    """Làm mới access_token bằng refresh_token qua REST. Trả tokens mới hoặc None."""
    import requests
    try:
        r = requests.post(
            f'{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token',
            headers={'apikey': SUPABASE_KEY, 'Content-Type': 'application/json'},
            json={'refresh_token': refresh_token}, timeout=8,
        )
        if r.status_code != 200:
            return None
        d = r.json()
        return {
            'access_token': d.get('access_token', ''),
            'refresh_token': d.get('refresh_token', refresh_token),
        }
    except Exception as e:
        logger.warning(f'refresh token REST lỗi: {e}')
        return None


def web_validate(access_token: str, refresh_token: str) -> dict:
    """Xác thực lại phiên với Supabase (giờ server, STATELESS đa user).

    Kiểm tra khóa/hết hạn/role LIVE. Nếu access_token hết hạn thì tự refresh.
    Trả {ok, role, expire_at, access_token, refresh_token} hoặc {ok:False, msg}.
    Lỗi mạng -> {ok:True, transient:True} (không đá user ra).
    """
    if not access_token or not refresh_token:
        return {'ok': False, 'msg': 'Thiếu token phiên.'}

    code, data = _rest_check_access(access_token)

    # access_token hết hạn (401) -> refresh rồi thử lại
    if code == 401:
        new = _rest_refresh(refresh_token)
        if not new or not new['access_token']:
            return {'ok': False, 'msg': 'Phiên đăng nhập đã hết hạn.'}
        access_token = new['access_token']
        refresh_token = new['refresh_token']
        code, data = _rest_check_access(access_token)

    if code == 0:
        # Lỗi mạng tạm thời -> giữ phiên
        return {'ok': True, 'transient': True}
    if code == 401 or code == 403:
        return {'ok': False, 'msg': 'Phiên đăng nhập không hợp lệ.'}
    if not isinstance(data, dict) or not data:
        return {'ok': False, 'msg': 'Tài khoản không tồn tại trên hệ thống!'}

    if data.get('blocked'):
        return {'ok': False, 'msg': 'Tài khoản của bạn đã bị khóa. Vui lòng liên hệ Admin!'}
    role = data.get('role', 'member')
    if role in ('banned', 'pending', 'disabled'):
        return {'ok': False, 'msg': f'Tài khoản đang ở trạng thái "{role}".'}
    if data.get('expired'):
        return {'ok': False, 'msg': 'Tài khoản của bạn đã hết hạn sử dụng!'}

    return {
        'ok': True, 'role': role, 'expire_at': data.get('expire_at'),
        'access_token': access_token, 'refresh_token': refresh_token,
    }


def _signout(client):
    try:
        client.auth.sign_out()
    except Exception:
        pass
