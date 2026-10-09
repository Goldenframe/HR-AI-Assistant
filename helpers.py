"""Corporate AI configuration and safe HTTP requests."""
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

class UserError(Exception):
    pass


def load_local_env(path):
    """Load supported local settings without overriding the process environment."""
    path = Path(path)
    if not path.is_file():
        return
    allowed = {'TELEGRAM_BOT_TOKEN', 'AI_BASE_URL', 'AI_API_KEY', 'AI_MODEL', 'HERMES_CONFIG'}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        key, sep, value = line.strip().partition('=')
        if not sep or key.strip() not in allowed:
            continue
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if value:
            os.environ.setdefault(key, value)


def save_bot_token(path, token):
    """Remember a validated token in the ignored local file, preserving other settings."""
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token):
        raise UserError('Неверный формат токена BotFather.')
    path = Path(path)
    try:
        lines = path.read_text(encoding='utf-8-sig').splitlines() if path.exists() else []
        lines = [line for line in lines if line.partition('=')[0].strip() != 'TELEGRAM_BOT_TOKEN']
        lines.append('TELEGRAM_BOT_TOKEN=' + token)
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        temporary.replace(path)
    except OSError:
        raise UserError('Не удалось сохранить токен в .env. Проверьте доступ к папке проекта.') from None


def request_json(url, payload=None, headers=None, timeout=40):
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={
        'Content-Type': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Never expose a Telegram URL (it contains the bot token) or response body.
        raise UserError(f'Сервис вернул HTTP {exc.code}. Проверьте доступ и ключ.') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise UserError('Не удалось связаться с сервисом. Проверьте интернет и Teleport.') from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise UserError('Сервис вернул ответ в неожиданном формате. Проверьте адрес подключения.') from None


def ai_settings():
    url, key = os.getenv('AI_BASE_URL'), os.getenv('AI_API_KEY')
    if not (url and key):
        config = Path(os.getenv('HERMES_CONFIG', str(
            Path(os.getenv('LOCALAPPDATA', '')) / 'hermes' / 'config.yaml')))
        if not config.is_file():
            raise UserError('Не найден конфиг Hermes. Задайте AI_BASE_URL и AI_API_KEY.')
        # Read only the existing flat rc-ai block, without loading unrelated secrets.
        section, active, values = '', False, {}
        for line in config.read_text(encoding='utf-8-sig').splitlines():
            if re.match(r'^\w[^:]*:', line):
                section, active = line.split(':', 1)[0], False
            if section != 'custom_providers':
                continue
            name = re.match(r'^\s*-\s*name:\s*(.*?)\s*$', line)
            if name:
                active = name.group(1).strip('"\'') == 'rc-ai'
            field = re.match(r'^\s*(base_url|api_key):\s*(.*?)\s*$', line)
            if active and field:
                value = field.group(2)
                if value.startswith('"'):
                    value = json.loads(value)
                else:
                    value = value.strip("'")
                values[field.group(1)] = value
        url, key = url or values.get('base_url'), key or values.get('api_key')
    if not url or not key or '<' in key:
        raise UserError('Не заполнены адрес или ключ корпоративного AI в Hermes.')
    if not url.startswith(('http://', 'https://')):
        raise UserError('Для AI нужен HTTP/HTTPS URL, а не tcp://.')
    return url.rstrip('/'), key, os.getenv('AI_MODEL', 'Qwen3-Coder-Next')

