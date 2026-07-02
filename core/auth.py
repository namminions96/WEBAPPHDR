"""
core/auth.py — HWID detection và auth status
"""
import uuid
import hashlib
import platform
import subprocess

from core.config import (
    BANK_ID, ACCOUNT_NO, ACCOUNT_NAME,
    PRICE_DAY, PRICE_MONTH, PRICE_PERM,
)
from core.telegram_notifier import send_telegram_notify


def get_hwid() -> str:
    try:
        system = platform.system()
        if system == 'Windows':
            result = subprocess.check_output(
                ['wmic', 'csproduct', 'get', 'UUID'], text=True
            )
            raw = result.strip().split()[-1]
        elif system == 'Darwin':
            # Dùng serial number máy Mac — ổn định, không thay đổi
            result = subprocess.check_output(
                ['system_profiler', 'SPHardwareDataType'], text=True
            )
            for line in result.splitlines():
                if 'Serial Number' in line:
                    raw = line.split(':')[-1].strip()
                    break
            else:
                raw = platform.node()
        else:
            raw = str(uuid.getnode())
        return hashlib.sha256(raw.encode()).hexdigest()[:32]
    except Exception:
        return hashlib.sha256(platform.node().encode()).hexdigest()[:32]


def check_auth_status() -> dict:
    """Trả về trạng thái auth — hardcoded admin/unlocked bypass."""
    return {
        'hwid': get_hwid(),
        'is_banned': False,
        'needs_registration': False,
        'is_admin': True,
        'fotello_unlocked': True,
        'autohdr_unlocked': True,
        'trial_active': False,
        'trial_remaining': 'Vĩnh viễn',
        'trial_expires_at': None,
        'fotello_remaining': 'Vĩnh viễn',
        'fotello_expires_at': None,
        'autohdr_remaining': 'Vĩnh viễn',
        'autohdr_expires_at': None,
        'payment_info': {
            'bank_id': BANK_ID,
            'account_no': ACCOUNT_NO,
            'account_name': ACCOUNT_NAME,
            'price_day': PRICE_DAY,
            'price_month': PRICE_MONTH,
            'price_perm': PRICE_PERM,
        },
    }


def register_device(name: str, fb_link: str) -> dict:
    hwid = get_hwid()
    send_telegram_notify(f'[Đăng ký] {name} | {fb_link} | hwid={hwid}')
    return {'ok': True, 'msg': 'Đăng ký thành công'}


def notify_payment(service: str, plan: str, amount: int, discount_code: str = '') -> None:
    hwid = get_hwid()
    msg = (
        f'[Thanh toán]\n'
        f'HWID: {hwid}\n'
        f'Dịch vụ: {service} / {plan}\n'
        f'Số tiền: {amount:,} VNĐ\n'
        f'Mã giảm giá: {discount_code or "Không"}'
    )
    send_telegram_notify(msg)
