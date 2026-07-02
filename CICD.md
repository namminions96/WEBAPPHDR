# CI/CD — Auto deploy lên server Linux

Workflow ở [.github/workflows/deploy.yml](.github/workflows/deploy.yml) chạy mỗi khi push lên
nhánh `main`:

1. **build-check**: build thử Docker image trên máy chủ GitHub Actions — nếu `Dockerfile` lỗi
   thì dừng ở đây, không đụng vào server thật.
2. **deploy**: SSH vào server Linux, `git reset --hard origin/main` để đồng bộ đúng code vừa
   push, rồi `docker compose up -d --build` để build lại image và restart container.

Cách này phù hợp với 1 VPS chạy `docker compose build: .` như hiện tại — không cần registry
(Docker Hub/GHCR), không cần đăng nhập gì thêm ngoài SSH.

## Chuẩn bị lần đầu trên server

```bash
# 1) Cài Docker + Docker Compose plugin (nếu chưa có)
curl -fsSL https://get.docker.com | sh

# 2) Clone repo về đúng 1 thư mục cố định — đây sẽ là DEPLOY_PATH
git clone https://github.com/namminions96/WEBAPPHDR.git /opt/webapphdr
cd /opt/webapphdr

# 3) Tạo file .env chứa secret thật (KHÔNG commit lên git)
cp .env.example .env
nano .env    # điền AUTOFOTELLO_SECRET = 1 chuỗi ngẫu nhiên dài, ví dụ: openssl rand -hex 32

# 4) Chạy thử 1 lần bằng tay để chắc mọi thứ OK
docker compose up -d --build
```

> ⚠️ Từ lúc này, **không sửa code trực tiếp trên server nữa** — mỗi lần CI/CD chạy sẽ
> `git reset --hard`, mọi thay đổi tay trong thư mục code (trừ `.env`, vì đã gitignore) sẽ bị
> mất. Sửa gì thì sửa ở máy dev, push lên GitHub, để CI/CD tự đồng bộ xuống.

## Tạo SSH key riêng cho GitHub Actions dùng

Không dùng key cá nhân của bạn — tạo 1 cặp key riêng chỉ để deploy:

```bash
# Chạy trên máy dev (hoặc trên server đều được)
ssh-keygen -t ed25519 -f deploy_key -N ""

# Copy public key vào server, thêm vào authorized_keys của user sẽ dùng để deploy
ssh-copy-id -i deploy_key.pub <user>@<server-ip>
# (hoặc dán tay nội dung deploy_key.pub vào ~/.ssh/authorized_keys trên server)
```

## Khai báo Secrets trên GitHub

Vào repo trên GitHub → **Settings → Secrets and variables → Actions → New repository secret**,
tạo 4 secret sau:

| Secret | Giá trị |
|---|---|
| `SSH_HOST` | IP hoặc domain của server |
| `SSH_USER` | user SSH dùng để deploy (vd `root`, `deploy`) |
| `SSH_PORT` | cổng SSH (bỏ qua nếu là `22`) |
| `SSH_PRIVATE_KEY` | toàn bộ nội dung file `deploy_key` (private key vừa tạo ở trên, **không phải** `.pub`) |
| `DEPLOY_PATH` | đường dẫn thư mục đã clone trên server, vd `/opt/webapphdr` |

Sau khi khai báo xong, chỉ cần `git push` lên nhánh `main` là workflow tự chạy. Xem tiến trình ở
tab **Actions** trên GitHub.

## Nếu server không mở SSH ra internet (đứng sau NAT/firewall)

Cách trên cần GitHub (máy chủ đám mây của GitHub Actions) SSH được vào server của bạn. Nếu
server không có IP public / không mở port SSH ra ngoài, dùng
[self-hosted runner](https://docs.github.com/en/actions/hosting-your-own-runners) cài thẳng lên
server thay vì SSH:

- Cài runner theo hướng dẫn của GitHub (Settings → Actions → Runners → New self-hosted runner).
- Đổi `runs-on: ubuntu-latest` của job `deploy` trong `deploy.yml` thành `runs-on: self-hosted`.
- Thay bước `appleboy/ssh-action` bằng chạy thẳng lệnh `git reset --hard` + `docker compose up -d --build`
  (vì lúc này job đã chạy ngay trên server, không cần SSH ra ngoài nữa).

## Rollback nếu deploy lỗi

```bash
ssh <user>@<server-ip>
cd /opt/webapphdr
git log --oneline -5              # tìm commit cũ còn ổn định
git reset --hard <commit-hash>
docker compose up -d --build
```
