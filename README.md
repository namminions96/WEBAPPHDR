# WEBAPPHDR

Bản web (Flask, multi-user) của AutoFotello/AutoHDR. Xem hướng dẫn chạy/deploy chi tiết trong [web/README.md](web/README.md).

## Chạy nhanh bằng Docker

```bash
cp .env.example .env   # điền AUTOFOTELLO_SECRET thật vào .env
docker compose up -d --build
```

Xem `docker-compose.yml` để cấu hình thêm biến môi trường khác.

## CI/CD — tự động deploy khi push lên `main`

Xem chi tiết cách cấu hình trong [CICD.md](CICD.md).
