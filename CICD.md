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

## Bước 3 — Khai báo secret `ENV_FILE` trên GitHub (chỉ 1 lần)

`.env` không copy tay lên server nữa — workflow tự tạo lại file này ở mỗi lần deploy từ 1 secret
tên `ENV_FILE`, nội dung y hệt 1 file `.env` bình thường (mỗi dòng 1 biến `KEY=value`).

Vào repo trên GitHub → **Settings → Secrets and variables → Actions → New repository secret**:

- Name: `ENV_FILE`
- Value: dán nguyên nội dung file `.env` (đủ 5 dòng `AUTOFOTELLO_SECRET`, `SUPABASE_URL`,
  `SUPABASE_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).

Chạy lại workflow (push commit mới, hoặc **Re-run jobs**) — bước *"Tạo file .env từ secret"* sẽ
ghi secret này ra `.env` trên server rồi build & chạy container.

> Muốn đổi giá trị nào (vd đổi `AUTOFOTELLO_SECRET`) thì sửa lại secret `ENV_FILE` trên GitHub rồi
> chạy lại workflow — không cần đụng gì trên server.

## Từ giờ trở đi

Chỉ cần `git push` lên `main` là server tự động:
`checkout code mới → docker compose up -d --build → dọn image cũ`.

Theo dõi tiến trình ở tab **Actions** trên GitHub — vì job `deploy` chạy ngay trên server, log
build/restart hiện trực tiếp ở đó, không cần SSH vào xem.

> ⚠️ Đừng sửa code (hay `.env`) trực tiếp trong thư mục `_work/WEBAPPHDR/WEBAPPHDR` trên server
> nữa — code thì sửa ở máy dev rồi push; `.env` thì sửa secret `ENV_FILE` trên GitHub — vì file
> `.env` trên server bị **ghi đè từ secret ở mỗi lần deploy**, sửa tay sẽ mất ngay lần deploy kế.

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
