"""Unattended entry point: always fictional team; no interactive credential prompt."""
import os
import re
import sys
from urllib.parse import urlparse

from helpers import UserError


def validate_environment(environ):
    token = environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token):
        raise UserError('Заполните TELEGRAM_BOT_TOKEN в .env.server. Значение не выводится в журнал.')
    parsed = urlparse(environ.get('AI_BASE_URL', ''))
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise UserError('Заполните AI_BASE_URL адресом корпоративной ИИ, доступным с сервера.')
    if not environ.get('AI_API_KEY', '').strip():
        raise UserError('Заполните AI_API_KEY в .env.server.')


def main():
    try:
        validate_environment(os.environ)
        # Explicit CLI flag takes precedence over any local or inherited TEAM_SOURCE.
        os.environ['TEAM_SOURCE'] = 'demo'
        sys.argv = [sys.argv[0], '--demo-team']
        from bot import main as run_bot
        run_bot()
    except UserError as exc:
        print(str(exc), flush=True)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
