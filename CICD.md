# CI/CD — Auto deploy lên server Linux (self-hosted runner)

Workflow ở [.github/workflows/deploy.yml](.github/workflows/deploy.yml) chạy mỗi khi push lên
nhánh `main`:

1. **build-check**: chạy trên máy chủ GitHub Actions (cloud) — build thử Docker image để chắc
   `Dockerfile` không lỗi. Không đụng gì tới server thật.
2. **deploy**: chạy **ngay trên server của bạn** (self-hosted runner) — checkout code mới nhất,
   rồi `docker compose up -d --build` để build lại image và restart container.

Vì job `deploy` chạy trực tiếp trên server (không phải SSH từ xa vào), không cần khai báo secret
SSH nào cả — chỉ cần cài runner 1 lần.

## Bước 1 — Cài Docker trên server (nếu chưa có)

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # để chạy docker không cần sudo — cần logout/login lại
```

## Bước 2 — Cài GitHub Actions self-hosted runner trên server

Vào repo trên GitHub → **Settings → Actions → Runners → New self-hosted runner**, chọn Linux,
làm theo đúng script GitHub hiển thị (đại khái):

```bash
mkdir actions-runner && cd actions-runner
curl -o actions-runner-linux-x64.tar.gz -L https://github.com/actions/runner/releases/download/vX.X.X/actions-runner-linux-x64-X.X.X.tar.gz
tar xzf actions-runner-linux-x64.tar.gz
./config.sh --url https://github.com/namminions96/WEBAPPHDR --token <TOKEN_GITHUB_CẤP>

# Cài làm service để tự chạy nền + tự khởi động lại cùng server:
sudo ./svc.sh install
sudo ./svc.sh start
```

> Dùng đúng token/URL mà GitHub hiển thị lúc bạn bấm "New self-hosted runner" — token có hạn
> dùng ngắn, hết hạn thì vào lại trang đó lấy token mới.

Sau khi cài xong, vào **Settings → Actions → Runners** sẽ thấy runner hiện trạng thái **Idle**
(màu xanh) — vậy là sẵn sàng nhận job.

## Bước 3 — Tạo `.env` (chỉ 1 lần, trực tiếp trên server)

Chạy thử workflow lần đầu (push 1 commit bất kỳ, hoặc bấm **Run workflow** thủ công) — bước
*"Kiểm tra .env đã có chưa"* sẽ báo lỗi và cho biết đường dẫn thư mục checkout (dạng
`~/actions-runner/_work/WEBAPPHDR/WEBAPPHDR`). Vào đúng thư mục đó trên server:

```bash
cd ~/actions-runner/_work/WEBAPPHDR/WEBAPPHDR
cp .env.example .env
nano .env    # điền AUTOFOTELLO_SECRET = 1 chuỗi ngẫu nhiên dài, ví dụ: openssl rand -hex 32
```

`.env` nằm ngoài git (đã gitignore) và bước checkout dùng `clean: false` nên file này sẽ **không
bị xóa** ở các lần deploy sau. Chạy lại workflow (push commit mới, hoặc **Re-run jobs**) — lần
này sẽ build & chạy container thành công.

## Từ giờ trở đi

Chỉ cần `git push` lên `main` là server tự động:
`checkout code mới → docker compose up -d --build → dọn image cũ`.

Theo dõi tiến trình ở tab **Actions** trên GitHub — vì job `deploy` chạy ngay trên server, log
build/restart hiện trực tiếp ở đó, không cần SSH vào xem.

> ⚠️ Đừng sửa code trực tiếp trong thư mục `_work/WEBAPPHDR/WEBAPPHDR` trên server nữa — sửa ở
> máy dev, push lên GitHub, để CI/CD tự đồng bộ xuống. Riêng `.env` thì sửa trực tiếp trên server
> là đúng (vì không nằm trong git).

## Rollback nếu deploy lỗi

```bash
cd ~/actions-runner/_work/WEBAPPHDR/WEBAPPHDR
git log --oneline -5              # tìm commit cũ còn ổn định
git reset --hard <commit-hash>
docker compose up -d --build
```

## Gỡ runner (nếu cần)

```bash
cd ~/actions-runner
sudo ./svc.sh stop
sudo ./svc.sh uninstall
./config.sh remove --token <TOKEN_GITHUB_CẤP>
```
