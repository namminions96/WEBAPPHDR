"""
web/run.py — Entry point chạy web server.

    python -m web.run           # chạy ở 0.0.0.0:8000
    PORT=5000 python -m web.run

Biến môi trường:
    PORT                  cổng (mặc định 8000)
    HOST                  host (mặc định 0.0.0.0)
    AUTOFOTELLO_SECRET    secret key cho Flask session (nên đặt cố định khi deploy)
"""
import os
import sys
import logging

# đảm bảo import được package gốc khi chạy trực tiếp
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Dùng trust store OS cho SSL (giống desktop) — hỗ trợ proxy/tường lửa doanh nghiệp.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

from web.server import app  # noqa: E402


def main():
    logging.basicConfig(level=logging.INFO)
    host = os.environ.get('HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', '8000'))
    debug = os.environ.get('DEBUG', '').lower() in ('1', 'true', 'yes')
    app.run(host=host, port=port, threaded=True, debug=debug, use_reloader=False)


if __name__ == '__main__':
    main()
