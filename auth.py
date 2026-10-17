"""Single-owner prototype access. Hosted credentials are always explicit."""
import hmac
import os
import secrets
from pathlib import Path
from werkzeug.security import check_password_hash


def hosted():
    return bool(os.environ.get('VERCEL'))


def configured():
    return not hosted() or all(os.environ.get(k, '').strip() for k in
        ('SECRET_KEY', 'DATABASE_URL', 'HERMES_USER', 'HERMES_PASSWORD_HASH'))


def secret_key():
    if os.environ.get('SECRET_KEY'): return os.environ['SECRET_KEY']
    if hosted(): return secrets.token_hex(32)  # Sign-in remains disabled.
    folder = Path(__file__).parent / '.instance'
    folder.mkdir(exist_ok=True)
    path = folder / 'session.key'
    if not path.exists(): path.write_text(secrets.token_hex(32), encoding='utf-8')
    return path.read_text(encoding='utf-8').strip()


def verify(username, password):
    expected_user = os.environ.get('HERMES_USER', '').strip()
    password_hash = os.environ.get('HERMES_PASSWORD_HASH', '')
    if expected_user and password_hash:
        return hmac.compare_digest(username, expected_user) and check_password_hash(password_hash, password)
    return not hosted() and hmac.compare_digest(password, os.environ.get('SITE_PIN', '24601'))
