"""
core/config.py — Hằng số toàn cục và cấu hình ứng dụng
"""
from pathlib import Path

CURRENT_VERSION = '1.4.5'
VERSION_CHECK_URL = "https://api.github.com/repos/namminions96/AutoHDR_APP/releases/latest"


TELEGRAM_BOT_TOKEN = '8643886685:AAF68GrHfgSbqJMLkogXdXlCN2C7fYX0lXE'
TELEGRAM_CHAT_ID   = '1345590928'

ACCOUNT_NAME = 'NGUYEN THI YEN NHI'
ACCOUNT_NO   = '44615107'
BANK_ID      = 'acb'

PRICE_DAY   = 100_000
PRICE_MONTH = 500_000
PRICE_PERM  = 1_500_000
TRIAL_HOURS = 12

import platform as _plt
import os as _os

def _detect_chrome_path() -> str:
    system = _plt.system()
    if system == 'Darwin':
        candidates = [
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            '/Applications/Chromium.app/Contents/MacOS/Chromium',
        ]
    elif system == 'Windows':
        candidates = [
            r'C:\Program Files\Google\Chrome\Application\chrome.exe',
            r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
            _os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe'),
        ]
    else:
        candidates = ['/usr/bin/google-chrome', '/usr/bin/chromium-browser']
    for path in candidates:
        if _os.path.exists(path):
            return path
    return candidates[0]

CHROME_PATH = _detect_chrome_path()
CHROME_PORT = 9222

APP_DATA_DIR = Path.home() / '.autofotello'
COOKIE_FILE  = APP_DATA_DIR / 'autohdr_cookie.json'

CLOUD_STYLES = [
    'original',
    'sunny_puffs',
    'loaded_puffs',
    'streaks_puffs',
    'sweep_streaks',
    'scatter_streaks',
    'crisp_streaks',
    'clear_fade',
]

CLOUD_STYLE_LABELS = {
    'original':       'Original',
    'sunny_puffs':    'Sunny Puffs',
    'loaded_puffs':   'Loaded Puffs',
    'streaks_puffs':  'Streaks w/ Puffs',
    'sweep_streaks':  'Sweep Streaks',
    'scatter_streaks':'Scatter Streaks',
    'crisp_streaks':  'Crisp Streaks',
    'clear_fade':     'Clear Fade',
}

MODEL_IDS = {
    'classic':          1,
    'classic_v4':       16,
    'lisa':             3,
    'twilight_golden':  6,
    'twilight_pink':    7,
    'twilight_midnight': 8,
}

MODEL_LABELS = {
    'classic':          'Classic',
    'classic_v4':       'Classic V4',
    'lisa':             'Lisa',
    'twilight_golden':  'Twilight – Golden',
    'twilight_pink':    'Twilight – Pink',
    'twilight_midnight':'Twilight – Midnight',
}

CACHE_TTL = 300  # seconds

# ──────────────────────────────────────────────────────────────
# GitHub Update Token (chỉ app desktop dùng để tải release từ repo private,
# web app không dùng tới -> bỏ trống trong bản deploy web)
# ──────────────────────────────────────────────────────────────
GITHUB_UPDATE_TOKEN = ""

# ──────────────────────────────────────────────────────────────
# Supabase Configuration
# ──────────────────────────────────────────────────────────────
SUPABASE_URL = "https://eijcdhlnfkfrvgcwpqcb.supabase.co"
SUPABASE_KEY = "sb_publishable_-QWzBkyVoqwZ_O-Ux0fEew_Nkxc7Zoj"
OAUTH_PORT = 54321

# Tên bảng profile + RPC. Để test trên bảng giả: đổi thành "profiles_v1" / "link_device_v1".
# Khi chạy thật: để "profiles" / "link_device".
PROFILES_TABLE = "profiles"
LINK_DEVICE_RPC = "link_device"
TRIAL_RPC = "ensure_trial_profile"  # bản thật: "ensure_trial_profile"
ACCESS_RPC = "check_access"          # bản thật: "check_access"

