"""Read-only Jira/Confluence participation matching for Server/Data Center."""
import json
import os
from hashlib import sha256
from pathlib import Path
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlparse

from helpers import UserError, JiraAccountError, request_json


def load_directory(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        employees, projects = data['employees'], data['projects']
        if not isinstance(employees, list) or not employees or not isinstance(projects, list) or not projects:
            raise ValueError()
        for e in employees:
            if (not re.fullmatch(r'[A-Za-z0-9_-]{1,20}', e['id']) or not e['name']
                    or e['role'] not in ('employee', 'hr')):
                raise ValueError()
            if e['role'] == 'employee':
                for field in ('jira_user', 'confluence_user'):
                    if e.get(field) is None and (field == 'confluence_user' or data.get('roster_source') == 'trck'):
                        continue
                    if not isinstance(e.get(field), str) or not e[field] or '<' in e[field]:
                        raise ValueError()
        for field in ('id', 'jira_user', 'confluence_user'):
            values = [e[field] for e in employees if field in e and e[field]]
            if len(values) != len(set(values)):
                raise ValueError()
        spaces = set()
        keys = set()
        for p in projects:
            if not re.fullmatch(r'[A-Z][A-Z0-9_]*', p['id']) or p['id'] in keys:
                raise ValueError()
            keys.add(p['id'])
            if not isinstance(p.get('spaces'), list):
                raise ValueError()
            for space in p['spaces']:
                if not isinstance(space, str) or not space or '<' in space or space in spaces:
                    raise ValueError()
                spaces.add(space)
            p['name'] = p.get('name') or p['id']
            p['members'] = []  # Membership is obtained from APIs, never from this file.
        return data
    except (OSError, ValueError, KeyError, TypeError):
        raise UserError('Заполните корпоративный каталог TEAM_DIRECTORY: профили, логины Jira/Confluence '
                        'и соответствие проектов пространствам. Образец: fixtures/corporate-team.example.json.') from None


class AtlassianMatcher:
    def __init__(self, directory, jira_url, jira_token, confluence_url, confluence_token, fetch=request_json):
        self.directory, self.fetch = directory, fetch
        for url in (jira_url, confluence_url):
            parsed = urlparse(url)
            if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
                raise UserError('Для Jira и Confluence нужен HTTP/HTTPS адрес приложения.')
        if not jira_token or not confluence_token:
            raise UserError('Задайте JIRA_PERSONAL_TOKEN и CONFLUENCE_PERSONAL_TOKEN в локальном .env.')
        self.jira_url, self.confluence_url = jira_url.rstrip('/'), confluence_url.rstrip('/')
        self.jira_headers = {'Authorization': 'Bearer ' + jira_token}
        self.confluence_headers = {'Authorization': 'Bearer ' + confluence_token}

    @classmethod
    def from_env(cls, root):
        path = Path(os.getenv('TEAM_DIRECTORY', 'data/corporate-team.json'))
        directory = load_directory(path if path.is_absolute() else root / path)
        return cls(directory, os.getenv('JIRA_URL', ''), os.getenv('JIRA_PERSONAL_TOKEN', ''),
                   os.getenv('CONFLUENCE_URL', ''), os.getenv('CONFLUENCE_PERSONAL_TOKEN', ''))

    @classmethod
    def import_from_env(cls, root):
        path = Path(os.getenv('TEAM_DIRECTORY', 'data/corporate-team.json'))
        path = path if path.is_absolute() else root / path
        try:
            previous = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {'employees': [], 'projects': []}
            if not isinstance(previous.get('employees'), list) or not isinstance(previous.get('projects'), list):
                raise ValueError()
        except (OSError, ValueError, AttributeError):
            raise UserError('Корпоративный каталог повреждён: проверьте формат JSON. Предыдущий файл не изменён.') from None
        client = cls(previous, os.getenv('JIRA_URL', ''), os.getenv('JIRA_PERSONAL_TOKEN', ''),
                     os.getenv('CONFLUENCE_URL', ''), os.getenv('CONFLUENCE_PERSONAL_TOKEN', ''))
        if os.getenv('DIRECTORY_SOURCE') == 'trck':
            from trck import Tracker
            directory = Tracker(root).directory(client, previous)
        else:
            directory = client.import_directory(previous)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(json.dumps(directory, ensure_ascii=False, indent=2), encoding='utf-8')
        # Validate before replacing the existing roster; errors preserve the old file.
        load_directory(temporary)
        temporary.replace(path)
        return directory

    def import_directory(self, previous=None):
        previous = previous or {'employees': [], 'projects': []}
        permissions = self.fetch(self.jira_url + '/rest/api/2/mypermissions?permissions=USER_PICKER',
                                 headers=self.jira_headers, timeout=20)
        if not permissions.get('permissions', {}).get('USER_PICKER', {}).get('havePermission'):
            raise UserError('Для загрузки сотрудников ключу Jira нужно право Browse users and groups. '
                            'Попросите администратора дать доступ для интеграции.')
        users, seen, start = [], set(), 0
        for _ in range(100):
            page = self.fetch(self.jira_url + '/rest/api/2/user/search?' + urlencode(
                {'username': '.', 'includeActive': 'true', 'includeInactive': 'false',
                 'startAt': start, 'maxResults': 100}), headers=self.jira_headers, timeout=20)
            if not isinstance(page, list):
                raise UserError('Jira вернула неверный список сотрудников.')
            if not page:
                break
            fresh = 0
            for user in page:
                if not isinstance(user, dict) or not user.get('name') or not user.get('key'):
                    raise UserError('В ответе Jira нет логинов или постоянных ключей пользователей.')
                if user['key'] in seen:
                    continue
                seen.add(user['key'])
                fresh += 1
                if user.get('active') is True:
                    users.append(user)
            if fresh == 0:
                raise UserError('Jira повторяет страницу сотрудников. Импорт остановлен, каталог не изменён.')
            start += len(page)
        else:
            raise UserError('Превышен лимит импорта сотрудников Jira.')
        if not users:
            raise UserError('Jira не вернула активных сотрудников. Проверьте доступ к каталогу.')
        # Some Jira versions cap a broad search at 100 entries. Keep previously
        # discovered accounts after verifying them individually, rather than losing profiles.
        for employee in previous['employees']:
            if not employee.get('jira_user') or (employee.get('jira_key') or employee['jira_user']) in seen:
                continue
            try:
                account = self.fetch(self.jira_url + '/rest/api/2/user?' + urlencode({'username': employee['jira_user']}),
                                     headers=self.jira_headers, timeout=20)
            except UserError as exc:
                if 'HTTP 404.' in str(exc):
                    continue
                raise
            if isinstance(account, dict) and account.get('active') is True and account.get('key') not in seen:
                if not account.get('name') or not account.get('key'):
                    raise UserError('Jira вернула неполный профиль сотрудника.')
                users.append(account)
                seen.add(account['key'])
        projects = self.fetch(self.jira_url + '/rest/api/2/project', headers=self.jira_headers, timeout=20)
        spaces, start = {}, 0
        for _ in range(100):
            page = self.fetch(self.confluence_url + '/rest/api/space?' + urlencode(
                {'type': 'global', 'start': start, 'limit': 100}), headers=self.confluence_headers, timeout=20)
            if not isinstance(page, dict) or not isinstance(page.get('results'), list):
                raise UserError('Confluence вернула неверный список пространств.')
            rows = page['results']
            for space in rows:
                if not isinstance(space, dict) or not space.get('key'):
                    raise UserError('Confluence вернула пространство без ключа.')
                spaces[space['key']] = space.get('name', space['key'])
            if not page.get('_links', {}).get('next'):
                break
            if not rows:
                raise UserError('Список пространств Confluence неполный.')
            start += len(rows)
        else:
            raise UserError('Превышен лимит импорта пространств Confluence.')
        # Separate directories may use different usernames. Match only a unique exact email,
        # never a person's display name; emails are used locally and are not saved in the roster.
        confluence_accounts, start = {}, 0
        for _ in range(100):
            page = self.fetch(self.confluence_url + '/rest/api/search?' + urlencode(
                {'cql': 'type=user', 'start': start, 'limit': 100}), headers=self.confluence_headers, timeout=20)
            if not isinstance(page, dict) or not isinstance(page.get('results'), list):
                raise UserError('Confluence вернула неверный каталог пользователей.')
            rows = page['results']
            fresh = 0
            for item in rows:
                account = item.get('user', {})
                username = account.get('username')
                if username and username not in confluence_accounts:
                    confluence_accounts[username] = account
                    fresh += 1
            start += len(rows)
            total = page.get('totalSize')
            more = start < total if type(total) is int else bool(page.get('_links', {}).get('next'))
            if not more:
                break
            if not rows or not fresh:
                raise UserError('Каталог пользователей Confluence неполный или повторяет страницу.')
        else:
            raise UserError('Превышен лимит импорта пользователей Confluence.')
        email_accounts = {}
        account_emails = {}
        for username in confluence_accounts:
            try:
                account = self.fetch(self.confluence_url + '/rest/api/user?' + urlencode({'username': username}),
                                     headers=self.confluence_headers, timeout=20)
            except UserError as exc:
                if 'HTTP 404.' in str(exc):
                    continue  # Deleted users can remain in the search index.
                raise
            email = account.get('email') if isinstance(account, dict) else None
            if isinstance(email, str) and email.strip():
                email = email.strip().casefold()
                account_emails[username] = email
                email_accounts.setdefault(email, []).append(username)
        jira_email_counts = {}
        for user in users:
            email = (user.get('emailAddress') or '').strip().casefold()
            if email:
                jira_email_counts[email] = jira_email_counts.get(email, 0) + 1
        old_users = {e.get('jira_key') or e.get('jira_user'): e for e in previous['employees'] if e.get('jira_user')}
        old_by_login = {e['jira_user']: e for e in previous['employees'] if e.get('jira_user')}
        employees = []
        for user in users:
            old = old_users.get(user['key']) or old_by_login.get(user['name'], {})
            confluence_user = old.get('confluence_user') or user['name']
            try:
                account = self.fetch(self.confluence_url + '/rest/api/user?' + urlencode({'username': confluence_user}),
                                     headers=self.confluence_headers, timeout=20)
            except UserError as exc:
                if 'HTTP 404.' in str(exc):
                    account = {}
                else:
                    raise
            # Do not guess by display name. Unmatched accounts still participate through Jira.
            confirmed = account.get('username') if isinstance(account, dict) else None
            if confirmed != confluence_user:
                confirmed = None
            email = (user.get('emailAddress') or '').strip().casefold()
            if email and len(email_accounts.get(email, [])) == 1 and jira_email_counts[email] == 1:
                confirmed = email_accounts[email][0]
            elif confirmed and email and account_emails.get(confirmed) and account_emails[confirmed] != email:
                confirmed = None
            employees.append({'id': old.get('id') or 'u' + sha256(user['key'].encode()).hexdigest()[:18],
                              'name': user.get('displayName') or user['name'], 'role': old.get('role', 'employee'),
                              'jira_user': user['name'], 'jira_key': user['key'], 'confluence_user': confirmed,
                              'project_keys': old.get('project_keys', [])})
        employees.extend(e for e in previous['employees'] if e['role'] == 'hr' and not e.get('jira_user'))
        if not any(e['role'] == 'hr' for e in employees):
            employees.append({'id': 'hr01', 'name': 'HR', 'role': 'hr'})
        old_projects = {p['id']: p for p in previous['projects'] if isinstance(p.get('spaces'), list)
                        and all(isinstance(s, str) and '<' not in s for s in p['spaces'])}
        imported_projects, used_spaces = [], set()
        if not isinstance(projects, list) or not projects:
            raise UserError('Jira не вернула доступные проекты.')
        for project in projects:
            if not isinstance(project, dict) or not project.get('key'):
                raise UserError('Jira вернула проект без ключа.')
            key = project['key']
            mapped = old_projects[key]['spaces'] if key in old_projects else ([key] if key in spaces else [])
            if any(space not in spaces for space in mapped):
                raise UserError('Настроенное пространство Confluence недоступно. Каталог не изменён.')
            used_spaces.update(mapped)
            imported_projects.append({'id': key, 'name': project.get('name') or key, 'spaces': mapped})
        return {'description': 'Автоматический импорт доступных активных аккаунтов Jira; проверьте служебные аккаунты.',
                'employees': employees, 'projects': imported_projects,
                'unmapped_spaces': [{'key': key, 'name': name} for key, name in spaces.items() if key not in used_spaces]}

    def jira_projects(self, user):
        # Historical assignments and logged work count; access permissions/watchers do not.
        name = json.dumps(user, ensure_ascii=False)
        keys = ','.join(json.dumps(p['id']) for p in self.directory['projects'])
        jql = f'project in ({keys}) AND (assignee WAS {name} OR reporter = {name} OR worklogAuthor = {name}) ORDER BY key'
        found, start = set(), 0
        for _ in range(100):
            response = self.fetch(self.jira_url + '/rest/api/2/search?' + urlencode(
                {'jql': jql, 'startAt': start, 'maxResults': 100, 'fields': 'project'}),
                headers=self.jira_headers, timeout=20)
            try:
                issues, total = response['issues'], response['total']
                if not isinstance(issues, list) or type(total) is not int or total < 0:
                    raise ValueError()
                found.update(i['fields']['project']['key'] for i in issues)
                start += len(issues)
                if start >= total:
                    return found
                if not issues:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise UserError('Jira вернула неполные данные участия. Подбор не выполнен.') from None
        raise UserError('Превышен лимит выборки Jira. Сузьте список проектов в корпоративном каталоге.')

    def project_people(self, project_keys):
        """Discover users from project activity when the global user picker is incomplete."""
        people = {}
        for project in project_keys:
            start = 0
            for _ in range(100):
                response = self.fetch(self.jira_url + '/rest/api/2/search?' + urlencode(
                    {'jql': 'project = ' + json.dumps(project) + ' ORDER BY key', 'startAt': start,
                     'maxResults': 100, 'fields': 'assignee,reporter,worklog'}), headers=self.jira_headers, timeout=20)
                issues, total = response.get('issues'), response.get('total')
                if not isinstance(issues, list) or type(total) is not int:
                    raise UserError('Не удалось прочитать участников проекта Jira.')
                for issue in issues:
                    fields = issue['fields']
                    actors = [fields.get('assignee'), fields.get('reporter')]
                    logs = fields.get('worklog') or {'worklogs': [], 'total': 0}
                    entries = logs.get('worklogs', [])
                    if logs.get('total', len(entries)) > len(entries):
                        raise UserError('В задачах есть усечённая история времени. Нужен полный каталог пользователей Jira.')
                    actors.extend(log.get('author') for log in entries)
                    for actor in actors:
                        if actor and actor.get('active') is True and actor.get('name') and actor.get('key'):
                            people[actor['key']] = actor
                start += len(issues)
                if start >= total:
                    break
                if not issues:
                    raise UserError('Jira вернула неполный список задач проекта.')
            else:
                raise UserError('Превышен лимит чтения участников проекта Jira.')
        return list(people.values())

    def confluence_projects(self, user):
        mapping = {space: p['id'] for p in self.directory['projects'] for space in p['spaces']}
        if not mapping:
            return set()
        cql = ('type = page AND contributor = ' + json.dumps(user, ensure_ascii=False)
               + ' AND space in (' + ','.join(json.dumps(s, ensure_ascii=False) for s in mapping) + ')')
        found, start = set(), 0
        for _ in range(100):
            response = self.fetch(self.confluence_url + '/rest/api/content/search?' + urlencode(
                {'cql': cql, 'start': start, 'limit': 100, 'expand': 'space'}),
                headers=self.confluence_headers, timeout=20)
            try:
                results = response['results']
                if not isinstance(results, list):
                    raise ValueError()
                for page in results:
                    space = page['space']['key']
                    if space in mapping:
                        found.add(mapping[space])
                if not response.get('_links', {}).get('next'):
                    return found
                if not results:
                    raise ValueError()
                start += len(results)
            except (KeyError, TypeError, ValueError):
                raise UserError('Confluence вернула неполные данные участия. Подбор не выполнен.') from None
        raise UserError('Превышен лимит выборки Confluence. Сузьте список пространств в каталоге.')

    def collect(self):
        # Network/auth/schema failures still cancel refresh. A single unsearchable
        # Jira account is omitted from that source, never from the employee catalog.
        configured = {p['id'] for p in self.directory['projects']}
        activity_cache, activity_lock = {}, threading.Lock()
        def participation(employee):
            jira, warnings = set(), []
            if employee.get('jira_user'):
                try:
                    jira = self.jira_projects(employee['jira_user'])
                except UserError as exc:
                    keys = set(employee.get('project_keys', [])) & configured
                    if not isinstance(exc, JiraAccountError) and not ('HTTP 400.' in str(exc) and keys and employee.get('jira_key')):
                        raise
                    # Known projects are re-read, rather than trusting stale membership.
                    if employee.get('jira_key'):
                        for project in sorted(keys):
                            with activity_lock:
                                if project not in activity_cache:
                                    activity_cache[project] = {u['key'] for u in self.project_people([project])}
                                if employee['jira_key'] in activity_cache[project]:
                                    jira.add(project)
                    if not jira:
                        warnings.append({'employee': employee['id'], 'service': 'Jira',
                                         'reason': 'account_not_searchable'})
            confluence = self.confluence_projects(employee['confluence_user']) if employee.get('confluence_user') else set()
            memberships = [{'employee': employee['id'], 'project': project,
                            'sources': (['Jira'] if project in jira else []) + (['Confluence'] if project in confluence else [])}
                           for project in sorted((jira | confluence) & configured)]
            return memberships, warnings
        employees = [e for e in self.directory['employees'] if e['role'] == 'employee']
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(participation, employees))
        return {'projects': self.directory['projects'],
                'memberships': [m for memberships, _ in results for m in memberships],
                'warnings': [w for _, warnings in results for w in warnings]}
