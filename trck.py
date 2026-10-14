"""Read-only employee roster from RedTrack; Jira accounts are not employees."""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from helpers import UserError, request_json

class Tracker:
    def __init__(self, root, fetch=request_json):
        self.root = Path(root)
        self.fetch = fetch
        self.url = os.getenv('TRCK_API_URL', 'https://trck.rdclr.ru').rstrip('/')
        if self.url != 'https://trck.rdclr.ru':
            raise UserError('Для корпоративного каталога нужен TRCK_API_URL=https://trck.rdclr.ru.')
        self.token_path = self.root / 'data/trck-tokens.json'

    def headers(self):
        try:
            tokens = json.loads(self.token_path.read_text(encoding='utf-8'))
            if tokens.get('expires_at', 0) <= time.time() + 30:
                endpoint = 'https://hello.rdclr.ru/realms/rc_realm/protocol/openid-connect/token'
                body = urlencode({'grant_type': 'refresh_token', 'client_id': 'web_tracker',
                                  'refresh_token': tokens['refresh_token']}).encode()
                with urlopen(Request(endpoint, data=body), timeout=20) as response:
                    tokens = json.load(response)
                tokens['expires_at'] = time.time() + tokens['expires_in']
                tmp = self.token_path.with_suffix('.tmp')
                tmp.write_text(json.dumps(tokens), encoding='utf-8')
                tmp.replace(self.token_path)
            return {'Authorization': 'Bearer ' + tokens['access_token']}
        except (OSError, ValueError, KeyError, HTTPError, URLError):
            raise UserError('Нужен повторный корпоративный вход в трекер: python tracker-login.py.') from None

    def employees(self):
        headers, rows, seen = self.headers(), [], set()
        total = None
        for number in range(100):
            page = self.fetch(self.url + '/api/v1/users?' + urlencode({
                'pageNumber': number, 'pageSize': 100, 'isActive': 'true', 'userType': 'EMPLOYEE'}),
                headers=headers, timeout=25)
            if (not isinstance(page, dict) or not isinstance(page.get('content'), list)
                    or type(page.get('totalElements')) is not int or page.get('number') != number):
                raise UserError('Трекер вернул неполный каталог сотрудников. Старый каталог сохранён.')
            if total is None:
                total = page['totalElements']
            if total != page['totalElements']:
                raise UserError('Каталог трекера изменился во время загрузки. Повторите импорт.')
            for row in page['content']:
                if not row.get('id') or not row.get('email') or not row.get('firstName') or not row.get('lastName'):
                    raise UserError('В трекере есть неполный профиль сотрудника. Импорт остановлен.')
                if row['id'] in seen:
                    raise UserError('Трекер повторил страницу сотрудников. Импорт остановлен.')
                seen.add(row['id'])
                rows.append({k: row[k] for k in ('id', 'firstName', 'lastName', 'email')})
            if page.get('last') is True:
                if len(rows) != total or not rows:
                    raise UserError('Трекер вернул неполный каталог сотрудников.')
                emails = [r['email'].strip().casefold() for r in rows]
                if len(set(emails)) != len(emails):
                    raise UserError('В трекере повторяются почтовые адреса. Требуется проверить профили.')
                return rows
            if not page['content']:
                raise UserError('Трекер вернул пустую промежуточную страницу.')
        raise UserError('Превышен лимит каталога трекера.')

    def directory(self, matcher, previous):
        people = self.employees()
        old_tracker = {str(e['trck_id']): e for e in previous['employees'] if e.get('trck_id')}
        old_jira = {e['jira_key']: e for e in previous['employees'] if e.get('jira_key')}
        def match(person):
            email = person['email'].strip().casefold()
            accounts = matcher.fetch(matcher.jira_url + '/rest/api/2/user/search?' + urlencode({
                'username': email, 'includeActive': 'true', 'includeInactive': 'false', 'maxResults': 100}),
                headers=matcher.jira_headers, timeout=25)
            if not isinstance(accounts, list):
                raise UserError('Jira вернула неверные данные сопоставления сотрудников.')
            exact = [a for a in accounts if a.get('active') is True and
                     (a.get('emailAddress') or '').strip().casefold() == email]
            exact = list({a.get('key'): a for a in exact if a.get('key') and a.get('name')}.values())
            # Ambiguous email must not select an arbitrary integration/person account.
            account = exact[0] if len(exact) == 1 else {}
            old = old_tracker.get(str(person['id'])) or old_jira.get(account.get('key'), {})
            return {'id': old.get('id') or 't' + sha256(str(person['id']).encode()).hexdigest()[:18],
                    'name': (person['firstName'] + ' ' + person['lastName']).strip(),
                    'role': old.get('role', 'employee'), 'trck_id': str(person['id']),
                    'jira_user': account.get('name'), 'jira_key': account.get('key'),
                    'confluence_user': old.get('confluence_user'), 'project_keys': old.get('project_keys', [])}
        with ThreadPoolExecutor(max_workers=6) as pool:
            employees = list(pool.map(match, people))
        # Keep a separately managed HR role; never grant it from a tracker role/name.
        employees.extend(e for e in previous['employees'] if e['role'] == 'hr' and not e.get('jira_user')
                         and e['id'] not in {p['id'] for p in employees})
        if not previous.get('projects'):
            raise UserError('Сначала настройте соответствие проектов Jira и пространств Confluence.')
        return {**previous, 'description': 'Действующие сотрудники из TRCK; сопоставление Jira по точной уникальной почте.', 'employees': employees, 'roster_source': 'trck',
                'roster_updated_at': time.time()}
