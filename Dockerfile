# Bản web AutoFotello — chạy trên Linux bằng gunicorn (1 worker, nhiều thread).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    AUTOFOTELLO_WEB_DATA=/data/users \
    AUTOFOTELLO_WEB_JOBS=/data/jobs \
    AUTOFOTELLO_PROXY=1

WORKDIR /app

# ca-certificates cho SSL; libgomp1 cho numpy/rawpy runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-web.txt .
RUN pip install -r requirements-web.txt

COPY . .

# Thư mục dữ liệu bền (token user, session, file tạm) — nên mount volume vào /data.
RUN mkdir -p /data/users /data/jobs
VOLUME ["/data"]

EXPOSE 8000

# QUAN TRỌNG: chỉ 1 worker (SSE + task nền + log-bus chạy in-process).
# Tăng tải bằng --threads, KHÔNG tăng -w.
CMD ["gunicorn", "web.server:app", \
     "-b", "0.0.0.0:8000", \
     "-w", "1", "-k", "gthread", "--threads", "24", \
     "--timeout", "300", "--graceful-timeout", "30", \
     "--access-logfile", "-", "--error-logfile", "-"]
