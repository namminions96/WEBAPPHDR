# AutoFotello — Bản Web (đa người dùng)

Bản web của app desktop (pywebview), giữ nguyên nghiệp vụ **upload/enhance/download** của Fotello & AutoHDR.
Thay vì mở Chrome trên máy user và lưu ảnh ra thư mục, bản web:

- Đăng nhập bằng **tài khoản Supabase** (email/mật khẩu hoặc Google OAuth) — **không khóa HWID**, đa người dùng.
- Kết nối Fotello/AutoHDR bằng **dán token/cookie thủ công** (mỗi user token riêng, lưu tách biệt theo user id).
- Upload ảnh từ trình duyệt (multipart).
- Tải kết quả về dưới dạng **ZIP** (mỗi project 1 file zip).

> UpCase và AutoEnhance hiện **chưa hỗ trợ trên web** (cần GPU/Chrome headless trên server) — sẽ làm sau.

## Chạy

```bash
pip install -r requirements.txt        # đã thêm flask
python -m web.run                      # mở http://localhost:8000
```

Biến môi trường:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `PORT` | `8000` | cổng web |
| `HOST` | `0.0.0.0` | địa chỉ bind |
| `AUTOFOTELLO_SECRET` | ngẫu nhiên | **đặt cố định khi deploy** (giữ phiên đăng nhập qua lần restart) |
| `AUTOFOTELLO_WEB_DATA` | `~/.autofotello/web_users` | nơi lưu token/cookie theo user |
| `AUTOFOTELLO_WEB_JOBS` | `~/.autofotello/web_jobs` | thư mục tạm cho upload/zip |
| `AUTOFOTELLO_MAX_UPLOAD` | 4 GiB | giới hạn dung lượng 1 lần upload |
| `AUTOFOTELLO_FILE_TTL` | 3600 | tự xóa file upload/zip tạm sau N giây (mặc định 1h) |

## Đăng nhập Google (OAuth)

Cần cấu hình trong Supabase → Authentication → URL Configuration → **Redirect URLs**, thêm:

```
http(s)://<domain-của-bạn>/auth/callback
```

Nếu chưa cấu hình, hãy dùng đăng nhập bằng **email/mật khẩu**.

## Kết nối Fotello / AutoHDR (dán token/cookie)

- **Fotello**: mở `app.fotello.co`, đăng nhập → DevTools (F12) → Console:
  `localStorage.getItem('refresh_token')` → copy dán vào ô kết nối.
- **AutoHDR**: mở `www.autohdr.com`, đăng nhập → DevTools → Network → copy header `Cookie` → dán vào.

## Kiến trúc

- `web/server.py` — Flask app + REST endpoints + SSE (`/api/events/<sid>`) cho log/tiến trình.
- `web/auth_web.py` — login Supabase (email/pass + Google OAuth), bỏ HWID.
- `web/user_store.py` — lưu token Fotello / cookie AutoHDR theo từng user id.
- `web/jobs.py` — thư mục tạm + đóng gói ZIP + token tải về.
- `web/templates/index.html`, `web/static/app.web.js` — UI (adapt từ `ui/`), gọi REST + SSE.
- Engines (`engines/*.py`) giữ nguyên; `fotello_engine` thêm tham số `state` để token tách theo user.

Bản desktop cũ (`main.py`, `app.py`, `ui/`) vẫn giữ nguyên, không ảnh hưởng.

## Deploy lên Linux

### ⚠️ Quan trọng: chỉ chạy **1 worker**, tăng tải bằng **threads**
SSE (`/api/events`), task chạy nền, và log-bus đều **in-process**. Nếu chạy nhiều worker
(gunicorn `-w >1`, hoặc nhiều container không sticky) thì log/tiến trình của task ở worker này
sẽ **không** tới được kết nối SSE ở worker khác. Muốn scale nhiều worker phải chuyển log-bus +
session sang store dùng chung (vd Redis) — ngoài phạm vi hiện tại.

### Cách 1 — Docker (khuyến nghị)
```bash
# build & chạy
docker compose up -d --build          # đọc docker-compose.yml
# hoặc chạy tay:
docker build -t autofotello-web .
docker run -d -p 8000:8000 -v autofotello_data:/data \
  -e AUTOFOTELLO_SECRET="chuoi-bi-mat-co-dinh" autofotello-web
```
- Nhớ đặt `AUTOFOTELLO_SECRET` **cố định** (giữ đăng nhập qua restart).
- Mount volume `/data` để giữ token user + session + zip tạm.
- Dockerfile đã dùng `gunicorn -w 1 -k gthread --threads 24`.

### Cách 2 — venv + gunicorn + systemd (không Docker)
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-web.txt
export AUTOFOTELLO_SECRET="chuoi-bi-mat-co-dinh"
export AUTOFOTELLO_PROXY=1            # nếu sau nginx
gunicorn web.server:app -b 0.0.0.0:8000 -w 1 -k gthread --threads 24 --timeout 300
```
Tạo service systemd `/etc/systemd/system/autofotello.service` trỏ tới lệnh gunicorn trên,
`WorkingDirectory` là thư mục dự án, thêm `Environment=` cho các biến.

### Nginx phía trước (bắt buộc chú ý SSE + upload lớn)
```nginx
server {
    listen 80;
    server_name your-domain.com;
    client_max_body_size 4g;                 # cho upload ảnh lớn/RAW

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;   # để url_for(https) đúng
    }
    location /api/events/ {                   # SSE: tắt buffering, timeout dài
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }
}
```
Khi phục vụ qua HTTPS: đặt `AUTOFOTELLO_PROXY=1` và `AUTOFOTELLO_HTTPS=1` để cookie có cờ `Secure`
và redirect Google OAuth ra đúng `https://.../auth/callback`.

### Sau khi deploy: cập nhật Redirect URL cho Google OAuth
Vào Supabase → Authentication → URL Configuration → Redirect URLs, thêm:
```
https://your-domain.com/auth/callback
```

### Biến môi trường bổ sung khi deploy
| Biến | Ý nghĩa |
|---|---|
| `AUTOFOTELLO_PROXY=1` | tin `X-Forwarded-*` khi chạy sau nginx (để scheme/host đúng) |
| `AUTOFOTELLO_HTTPS=1` | bật cờ `Secure` cho cookie (khi dùng https) |
| `AUTOFOTELLO_SESSION_DAYS` | số ngày giữ đăng nhập (mặc định 7) |
