"""
core/supabase_auth.py — Supabase Authentication & Google OAuth helper
"""
import os
import json
import time
import logging
import threading
import webbrowser
import http.server
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

from core.auth import get_hwid
from core.config import (
    SUPABASE_URL, SUPABASE_KEY, OAUTH_PORT, APP_DATA_DIR,
    PROFILES_TABLE, LINK_DEVICE_RPC, TRIAL_RPC, ACCESS_RPC,
)

logger = logging.getLogger(__name__)
_supabase_client = None


def get_supabase_client():
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    if (not SUPABASE_URL 
            or "your-project-id" in SUPABASE_URL 
            or not SUPABASE_KEY 
            or "your-anon-or-service" in SUPABASE_KEY):
        logger.warning("Supabase URL or Key is not configured. Supabase Auth will not work.")
        return None

    try:
        from supabase import create_client
        # Tắt auto-refresh chạy nền để tránh xoay refresh_token mà app không lưu kịp.
        # Việc refresh do set_session() kiểm soát rõ ràng, token mới được lưu lại file.
        try:
            from supabase.lib.client_options import ClientOptions
            options = ClientOptions(auto_refresh_token=False)
            _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY, options)
        except Exception:
            # Fallback nếu version supabase-py khác signature
            _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
        return _supabase_client
    except Exception as e:
        logger.error(f"Error initializing Supabase client: {e}")
        return None


def is_user_expired(expire_at_str: str) -> bool:
    """Kiểm tra thời gian hết hạn với UTC timezone."""
    if not expire_at_str:
        return False  # Không có thời hạn = Vĩnh viễn
    try:
        clean_str = expire_at_str.replace('Z', '+00:00')
        expire_dt = datetime.fromisoformat(clean_str)
        now = datetime.now(timezone.utc)
        return now > expire_dt
    except Exception as e:
        logger.error(f"Error parsing expire_at '{expire_at_str}': {e}")
        return False


def check_app_login() -> dict:
    """Đọc session và kiểm tra đăng nhập/hết hạn trên Supabase."""
    session_file = APP_DATA_DIR / 'app_session.json'
    if not session_file.exists():
        return {'ok': False}

    try:
        with open(session_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if not data.get('logged_in'):
            return {'ok': False}

        email = data.get('email')
        role = data.get('role', 'member')
        expire_at = data.get('expire_at')
        access_token = data.get('access_token')
        refresh_token = data.get('refresh_token')

        # 1. Kiểm tra hạn dùng cục bộ
        if is_user_expired(expire_at):
            session_file.unlink(missing_ok=True)
            return {'ok': False, 'msg': 'Tài khoản của bạn đã hết hạn sử dụng!'}

        # 2. Đồng bộ/Kiểm tra với Supabase
        client = get_supabase_client()
        if client and access_token and refresh_token:
            try:
                set_res = client.auth.set_session(access_token, refresh_token)
                # set_session có thể tự refresh + xoay refresh_token -> hứng token mới
                if getattr(set_res, 'session', None):
                    access_token = set_res.session.access_token or access_token
                    refresh_token = set_res.session.refresh_token or refresh_token
                user_res = client.auth.get_user()
                user = user_res.user

                # Gọi RPC server-side: hết hạn tính bằng now() của Supabase
                # -> user đổi đồng hồ máy KHÔNG bypass được khi online.
                access = client.rpc(ACCESS_RPC).execute().data
                if not access:
                    # Profile không tồn tại -> Đã bị xoá
                    session_file.unlink(missing_ok=True)
                    try:
                        client.auth.sign_out()
                    except Exception:
                        pass
                    return {'ok': False, 'msg': 'Tài khoản không tồn tại trên hệ thống!'}

                role = access.get('role', 'member')
                expire_at = access.get('expire_at')
                db_hwid = access.get('hwid')
                db_email = access.get('email')
                is_blocked = access.get('blocked')
                server_expired = access.get('expired')

                # Kiểm tra tài khoản bị khóa (block riêng, độc lập với role)
                if is_blocked:
                    session_file.unlink(missing_ok=True)
                    try:
                        client.auth.sign_out()
                    except Exception:
                        pass
                    return {'ok': False, 'msg': 'Tài khoản của bạn đã bị khóa. Vui lòng liên hệ Admin!'}

                # Cap nhat email neu chua co hoac bi thay doi
                if not db_email or db_email != email:
                    try:
                        client.table(PROFILES_TABLE).update({"email": email}).eq("id", user.id).execute()
                    except Exception as email_err:
                        logger.warning(f"Loi tu dong cap nhat email khi check login: {email_err}")

                # Kiểm tra khóa HWID thiết bị (Tránh copy session file sang máy khác)
                current_hwid = get_hwid()
                if db_hwid and db_hwid != current_hwid:
                    session_file.unlink(missing_ok=True)
                    try:
                        client.auth.sign_out()
                    except Exception:
                        pass
                    return {'ok': False, 'msg': 'Thiết bị này không khớp với tài khoản liên kết!'}

                # Kiểm tra trạng thái tài khoản
                if role in ('banned', 'pending', 'disabled'):
                    session_file.unlink(missing_ok=True)
                    try:
                        client.auth.sign_out()
                    except Exception:
                        pass
                    return {'ok': False, 'msg': f'Tài khoản đang ở trạng thái "{role}". Vui lòng liên hệ Admin!'}

                # Kiểm tra hạn dùng (dùng cờ server tính -> chống đổi đồng hồ máy)
                if server_expired:
                    session_file.unlink(missing_ok=True)
                    try:
                        client.auth.sign_out()
                    except Exception:
                        pass
                    return {'ok': False, 'msg': 'Tài khoản của bạn đã hết hạn sử dụng!'}

                # Lấy token mới nhất từ client (phòng khi auto-refresh đã xoay token nền)
                try:
                    cur_session = client.auth.get_session()
                    if cur_session:
                        access_token = cur_session.access_token or access_token
                        refresh_token = cur_session.refresh_token or refresh_token
                except Exception:
                    pass

                # Cập nhật lại session local (LƯU LẠI token mới để lần sau không bị invalid)
                data['role'] = role
                data['expire_at'] = expire_at
                data['access_token'] = access_token
                data['refresh_token'] = refresh_token
                with open(session_file, 'w', encoding='utf-8') as sf:
                    json.dump(data, sf, ensure_ascii=False, indent=2)

            except Exception as e:
                err_str = str(e).lower()
                # Phân biệt lỗi xác thực/tài khoản bị vô hiệu hoá vs lỗi mạng
                auth_errors = ("invalid", "expired", "not found", "400", "401", "403", "revoked", "disabled", "banned")
                is_auth_error = any(ae in err_str for ae in auth_errors)
                
                if is_auth_error or ("connection" not in err_str and "timeout" not in err_str and "dns" not in err_str):
                    logger.error(f"Supabase auth check rejected session: {e}")
                    session_file.unlink(missing_ok=True)
                    try:
                        client.auth.sign_out()
                    except Exception:
                        pass
                    return {'ok': False, 'msg': 'Phiên đăng nhập đã hết hạn hoặc tài khoản không hợp lệ!'}
                else:
                    # Lỗi mạng thực sự -> Cho phép dùng offline nếu session cũ chưa hết hạn
                    logger.warning(f"Lỗi kết nối Supabase, cho phép dùng cache offline: {e}")

        return {
            'ok': True,
            'username': email,  # JS app.js dùng thuộc tính username để hiển thị
            'email': email,
            'role': role,
            'expire_at': expire_at
        }
    except Exception as e:
        logger.error(f"Error checking app login: {e}")
        return {'ok': False}


def perform_app_logout() -> dict:
    """Xóa file session local."""
    session_file = APP_DATA_DIR / 'app_session.json'
    if session_file.exists():
        try:
            session_file.unlink(missing_ok=True)
        except Exception:
            pass
    return {'ok': True}


# ──────────────────────────────────────────────────────────────
# OAuth Callback Server
# ──────────────────────────────────────────────────────────────

class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Tắt log console để tránh làm rối log app
        pass

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)

        # Route redirect chính từ Supabase
        if parsed_url.path == '/callback':
            query = urllib.parse.parse_qs(parsed_url.query)
            code = query.get('code', [None])[0]

            # Nếu là PKCE flow (có code trong query params)
            if code:
                self.server.auth_code = code
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()

                html = """
                <!DOCTYPE html>
                <html>
                <head>
                    <title>Neatimage Tool - Đăng nhập thành công</title>
                    <meta charset="utf-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1">
                    <style>
                        body {
                            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                            display: flex;
                            align-items: center;
                            justify-content: center;
                            height: 100vh;
                            margin: 0;
                            background: #f7f9fc;
                            color: #333;
                        }
                        .card {
                            background: white;
                            padding: 32px;
                            border-radius: 12px;
                            box-shadow: 0 4px 20px rgba(0,0,0,0.05);
                            text-align: center;
                            max-width: 420px;
                            width: 90%;
                        }
                        h2 { color: #16a34a; margin-top: 0; font-size: 20px; }
                        p { font-size: 14px; color: #666; line-height: 1.5; }
                    </style>
                </head>
                <body>
                    <div class="card">
                        <h2>Đăng nhập thành công!</h2>
                        <p>Bạn đã đăng nhập thành công vào Neatimage Tool.<br>Có thể đóng trình duyệt này và quay lại ứng dụng.</p>
                    </div>
                    <script>
                        setTimeout(() => {
                            window.open('', '_self', '');
                            window.close();
                        }, 1500);
                    </script>
                </body>
                </html>
                """
                self.wfile.write(html.encode('utf-8'))
                return

            # Nếu là implicit flow (fallback lấy hash fragment)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()

            html = """
            <!DOCTYPE html>
            <html>
            <head>
                <title>Neatimage Tool - Đang xác thực</title>
                <meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <style>
                    body {
                        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        height: 100vh;
                        margin: 0;
                        background: #f7f9fc;
                        color: #333;
                    }
                    .card {
                        background: white;
                        padding: 32px;
                        border-radius: 12px;
                        box-shadow: 0 4px 20px rgba(0,0,0,0.05);
                        text-align: center;
                        max-width: 420px;
                        width: 90%;
                    }
                    h2 { color: #4f46e5; margin-top: 0; font-size: 20px; }
                    .spinner {
                        border: 3px solid #f3f3f3;
                        border-top: 3px solid #4f46e5;
                        border-radius: 50%;
                        width: 32px;
                        height: 32px;
                        animation: spin 1s linear infinite;
                        margin: 24px auto;
                    }
                    p { font-size: 14px; color: #666; line-height: 1.5; }
                    @keyframes spin {
                        0% { transform: rotate(0deg); }
                        100% { transform: rotate(360deg); }
                    }
                </style>
            </head>
            <body>
                <div class="card">
                    <h2 id="status-title">Đang xác thực tài khoản...</h2>
                    <div id="spinner" class="spinner"></div>
                    <p id="status-desc">Vui lòng chờ trong giây lát khi ứng dụng nhận thông tin đăng nhập.</p>
                </div>
                <script>
                    const hash = window.location.hash;
                    if (hash) {
                        const params = new URLSearchParams(hash.replace('#', '?'));
                        const accessToken = params.get('access_token');
                        const refreshToken = params.get('refresh_token');
                        
                        if (accessToken) {
                            fetch('/token?access_token=' + encodeURIComponent(accessToken) + '&refresh_token=' + encodeURIComponent(refreshToken))
                                .then(res => {
                                    document.getElementById('status-title').innerText = "Đăng nhập thành công!";
                                    document.getElementById('status-title').style.color = "#16a34a";
                                    document.getElementById('spinner').style.display = "none";
                                    document.getElementById('status-desc').innerHTML = "Bạn đã đăng nhập thành công vào Neatimage Tool.<br>Có thể đóng trình duyệt này và quay lại ứng dụng.";
                                    setTimeout(() => {
                                        window.open('', '_self', '');
                                        window.close();
                                    }, 1500);
                                })
                                .catch(err => {
                                    showError("Không thể gửi mã xác thực về ứng dụng.");
                                });
                        } else {
                            showError("Không nhận được mã access_token.");
                        }
                    } else {
                        const queryParams = new URLSearchParams(window.location.search);
                        const errorDesc = queryParams.get('error_description');
                        if (errorDesc) {
                            showError(decodeURIComponent(errorDesc));
                        } else {
                            showError("Không nhận được phản hồi đăng nhập.");
                        }
                    }

                    function showError(msg) {
                        document.getElementById('status-title').innerText = "Đăng nhập thất bại!";
                        document.getElementById('status-title').style.color = "#dc2626";
                        document.getElementById('spinner').style.display = "none";
                        document.getElementById('status-desc').innerText = msg;
                    }
                </script>
            </body>
            </html>
            """
            self.wfile.write(html.encode('utf-8'))

        # API nhận token từ client-side JS
        elif parsed_url.path == '/token':
            query = urllib.parse.parse_qs(parsed_url.query)
            access_token = query.get('access_token', [None])[0]
            refresh_token = query.get('refresh_token', [None])[0]

            if access_token:
                self.server.access_token = access_token
                self.server.refresh_token = refresh_token
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(b"OK")
            else:
                self.send_response(400)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(b"Missing token")
        else:
            self.send_response(404)
            self.end_headers()


class OAuthServer(http.server.HTTPServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.access_token = None
        self.refresh_token = None
        self.auth_code = None


def perform_google_login() -> dict:
    """Khởi chạy OAuth login bằng trình duyệt."""
    client = get_supabase_client()
    if not client:
        return {
            'ok': False,
            'msg': 'Supabase chưa được cấu hình. Vui lòng điền SUPABASE_URL và SUPABASE_KEY trong file config.py!'
        }

    server = None
    try:
        # Khởi động HTTP server nhận token
        server = OAuthServer(('127.0.0.1', OAUTH_PORT), OAuthCallbackHandler)
        
        # Chạy server ở thread phụ
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        # Tạo link đăng nhập Google OAuth từ Supabase
        res = client.auth.sign_in_with_oauth({
            "provider": "google",
            "options": {
                "redirect_to": f"http://localhost:{OAUTH_PORT}/callback"
            }
        })
        oauth_url = res.url

        # Mở trình duyệt
        webbrowser.open(oauth_url)

        # Chờ nhận token hoặc auth_code trong 120s
        access_token = None
        refresh_token = None
        auth_code = None
        deadline = time.time() + 120
        while time.time() < deadline:
            if server.access_token or server.auth_code:
                access_token = server.access_token
                refresh_token = server.refresh_token
                auth_code = server.auth_code
                break
            time.sleep(0.5)

        if not access_token and not auth_code:
            return {'ok': False, 'msg': 'Hết thời gian chờ đăng nhập trên trình duyệt.'}

        # Nếu nhận được auth_code (PKCE), tiến hành trao đổi mã lấy session
        if auth_code:
            try:
                auth_res = client.auth.exchange_code_for_session({"auth_code": auth_code})
                access_token = auth_res.session.access_token
                refresh_token = auth_res.session.refresh_token
            except Exception as e:
                return {'ok': False, 'msg': f'Lỗi xác thực mã (PKCE): {e}'}
        else:
            # Lưu phiên đăng nhập implicit vào client Supabase
            client.auth.set_session(access_token, refresh_token)
        user_res = client.auth.get_user()
        user = user_res.user
        email = user.email

        # Lấy thông tin profiles từ database
        has_profile = False
        role = 'member'
        expire_at = None
        db_hwid = None
        db_email = None
        is_blocked = False
        try:
            profile_res = client.table(PROFILES_TABLE).select('role, expire_at, hwid, email, blocked').eq('id', user.id).execute()
            if profile_res.data:
                profile = profile_res.data[0]
                role = profile.get('role', 'member')
                expire_at = profile.get('expire_at')
                db_hwid = profile.get('hwid')
                db_email = profile.get('email')
                is_blocked = profile.get('blocked')
                has_profile = True
                
                # Cap nhat email neu chua co hoac bi thay doi
                if not db_email or db_email != email:
                    try:
                        client.table(PROFILES_TABLE).update({"email": email}).eq("id", user.id).execute()
                    except Exception as email_err:
                        logger.warning(f"Loi cap nhat email: {email_err}")
            else:
                # Chưa có profile -> gọi RPC server-side tạo trial 1 ngày + link máy luôn.
                # Việc tạo do DB kiểm soát (role/hạn dùng cố định) -> user không forge được.
                current_hwid = get_hwid()
                trial_status = None
                try:
                    trial_res = client.rpc(TRIAL_RPC, {'p_hwid': current_hwid}).execute()
                    trial_status = trial_res.data
                except Exception as trial_err:
                    logger.warning(f"Lỗi gọi RPC tạo trial: {trial_err}")

                if trial_status == 'device_taken':
                    try:
                        client.auth.sign_out()
                    except Exception:
                        pass
                    return {'ok': False, 'msg': 'Thiết bị này đã dùng thử rồi. Vui lòng liên hệ Admin để gia hạn!'}

                if trial_status in ('created', 'exists'):
                    # Đọc lại profile vừa tạo
                    recheck = client.table(PROFILES_TABLE).select('role, expire_at, hwid, email, blocked').eq('id', user.id).execute()
                    if recheck.data:
                        profile = recheck.data[0]
                        role = profile.get('role', 'member')
                        expire_at = profile.get('expire_at')
                        db_hwid = profile.get('hwid')
                        db_email = profile.get('email')
                        is_blocked = profile.get('blocked')
                        has_profile = True
        except Exception as e:
            logger.warning(f"Không thể đọc bảng profiles: {e}")

        # Kiểm tra điều kiện kích hoạt tài khoản
        if not has_profile:
            try:
                client.auth.sign_out()
            except Exception:
                pass
            return {'ok': False, 'msg': 'Tài khoản Google này chưa được kích hoạt hoặc cấp phép sử dụng!'}

        # Kiểm tra khóa HWID (1 máy chỉ dùng 1 tài khoản, 1 tài khoản chỉ dùng 1 máy)
        current_hwid = get_hwid()
        if not db_hwid:
            # Tài khoản chưa liên kết thiết bị nào -> Gọi RPC server-side để liên kết.
            # Việc ghi hwid KHÔNG làm từ client (tránh user tự forge/đổi máy).
            try:
                rpc_res = client.rpc(LINK_DEVICE_RPC, {'p_hwid': current_hwid}).execute()
                status = rpc_res.data
                if status == 'device_taken':
                    try:
                        client.auth.sign_out()
                    except Exception:
                        pass
                    return {'ok': False, 'msg': 'Thiết bị này đã được liên kết với một tài khoản khác!'}
                if status == 'already_linked':
                    # Tài khoản đã có máy khác (race condition) -> đọc lại để so sánh
                    try:
                        recheck = client.table(PROFILES_TABLE).select('hwid').eq('id', user.id).execute()
                        db_hwid = recheck.data[0].get('hwid') if recheck.data else None
                    except Exception:
                        db_hwid = None
                    if db_hwid and db_hwid != current_hwid:
                        try:
                            client.auth.sign_out()
                        except Exception:
                            pass
                        return {'ok': False, 'msg': 'Tài khoản này đã được liên kết với một thiết bị khác!'}
                elif status == 'ok':
                    db_hwid = current_hwid
            except Exception as hw_err:
                logger.warning(f"Lỗi gọi RPC link_device: {hw_err}")
                try:
                    client.auth.sign_out()
                except Exception:
                    pass
                return {'ok': False, 'msg': 'Không thể liên kết thiết bị. Vui lòng thử lại!'}
        else:
            # Tài khoản đã liên kết thiết bị khác trước đó
            if db_hwid != current_hwid:
                try:
                    client.auth.sign_out()
                except Exception:
                    pass
                return {'ok': False, 'msg': 'Tài khoản này đã được liên kết với một thiết bị khác!'}

        if role in ('banned', 'pending', 'disabled'):
            try:
                client.auth.sign_out()
            except Exception:
                pass
            return {'ok': False, 'msg': f'Tài khoản đang ở trạng thái "{role}". Vui lòng liên hệ Admin!'}

        # Kiểm tra tài khoản bị khóa (block riêng, độc lập với role)
        if is_blocked:
            try:
                client.auth.sign_out()
            except Exception:
                pass
            return {'ok': False, 'msg': 'Tài khoản của bạn đã bị khóa. Vui lòng liên hệ Admin!'}

        # Kiểm tra hết hạn ngay lúc đăng nhập — dùng giờ SERVER (chống đổi đồng hồ máy)
        try:
            access = client.rpc(ACCESS_RPC).execute().data
            server_expired = bool(access.get('expired')) if access else False
        except Exception as exp_err:
            logger.warning(f"Lỗi kiểm tra hết hạn server-side, fallback giờ local: {exp_err}")
            server_expired = is_user_expired(expire_at)
        if server_expired:
            try:
                client.auth.sign_out()
            except Exception:
                pass
            return {'ok': False, 'msg': 'Tài khoản của bạn đã hết hạn sử dụng!'}

        # Cap nhat thoi gian dang nhap lan cuoi
        try:
            client.table(PROFILES_TABLE).update({"last_login_at": datetime.now(timezone.utc).isoformat()}).eq("id", user.id).execute()
        except Exception as up_err:
            logger.warning(f"Loi cap nhat last_login_at: {up_err}")

        # Ghi file session cục bộ
        session_file = APP_DATA_DIR / 'app_session.json'
        session_data = {
            'logged_in': True,
            'email': email,
            'role': role,
            'expire_at': expire_at,
            'access_token': access_token,
            'refresh_token': refresh_token,
            'saved_at': datetime.now(timezone.utc).isoformat()
        }
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(session_file, 'w', encoding='utf-8') as f:
            json.dump(session_data, f, ensure_ascii=False, indent=2)

        return {
            'ok': True,
            'username': email,
            'email': email,
            'role': role,
            'expire_at': expire_at
        }

    except Exception as e:
        logger.error(f"Lỗi đăng nhập Google OAuth: {e}")
        return {'ok': False, 'msg': f'Lỗi đăng nhập: {e}'}

    finally:
        if server:
            server.shutdown()
            server.server_close()
