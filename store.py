"""SQLite storage, enrollment, colleague matching and report permissions."""
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import secrets
import sqlite3
import time

from helpers import UserError
from survey import QUESTIONNAIRE, LEGACY_QUESTIONNAIRE

MIN_RESPONSES = 3


class Store:
    def __init__(self, path, fixture=None):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS employees (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
                    telegram_id INTEGER UNIQUE, enrollment_hash TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS memberships (
                    employee TEXT REFERENCES employees(id), project TEXT REFERENCES projects(id),
                    PRIMARY KEY(employee,project));
                CREATE TABLE IF NOT EXISTS campaigns (
                    id TEXT PRIMARY KEY, requester INTEGER NOT NULL,
                    target TEXT REFERENCES employees(id), state TEXT NOT NULL DEFAULT 'draft',
                    demo INTEGER NOT NULL DEFAULT 0, created TEXT DEFAULT CURRENT_TIMESTAMP,
                    questionnaire TEXT);
                CREATE TABLE IF NOT EXISTS invitations (
                    campaign TEXT REFERENCES campaigns(id), employee TEXT REFERENCES employees(id),
                    delivery TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt REAL NOT NULL DEFAULT 0, PRIMARY KEY(campaign,employee));
                CREATE TABLE IF NOT EXISTS responses (
                    campaign TEXT REFERENCES campaigns(id), employee TEXT REFERENCES employees(id),
                    answers TEXT NOT NULL, PRIMARY KEY(campaign,employee));
                CREATE TABLE IF NOT EXISTS sessions (
                    user INTEGER PRIMARY KEY, campaign TEXT REFERENCES campaigns(id), answers TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts (
                    user INTEGER, message_id INTEGER, step INTEGER, PRIMARY KEY(user,message_id));
                CREATE TABLE IF NOT EXISTS reports (
                    campaign TEXT PRIMARY KEY REFERENCES campaigns(id), count INTEGER, report TEXT);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
            ''')
            campaign_columns = {r['name'] for r in db.execute('PRAGMA table_info(campaigns)')}
            if 'questionnaire' not in campaign_columns:
                db.execute('ALTER TABLE campaigns ADD COLUMN questionnaire TEXT')
            db.execute('UPDATE campaigns SET questionnaire=? WHERE questionnaire IS NULL',
                       (json.dumps(LEGACY_QUESTIONNAIRE, ensure_ascii=False),))
            columns = {r['name'] for r in db.execute('PRAGMA table_info(invitations)')}
            if 'attempts' not in columns:
                db.execute('ALTER TABLE invitations ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0')
            if 'last_attempt' not in columns:
                db.execute('ALTER TABLE invitations ADD COLUMN last_attempt REAL NOT NULL DEFAULT 0')
            db.execute("UPDATE invitations SET delivery='waiting' WHERE delivery='manual'")
            if fixture and not db.execute('SELECT 1 FROM employees LIMIT 1').fetchone():
                data = json.loads(Path(fixture).read_text(encoding='utf-8'))
                for e in data['employees']:
                    if e['role'] not in ('employee', 'hr'):
                        raise ValueError('Invalid role in fixture')
                    db.execute('INSERT INTO employees(id,name,role) VALUES(?,?,?)',
                               (e['id'], e['name'], e['role']))
                for p in data['projects']:
                    db.execute('INSERT INTO projects VALUES(?,?)', (p['id'], p['name']))
                    for member in p['members']:
                        db.execute('INSERT INTO memberships VALUES(?,?)', (member, p['id']))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def enrollment_codes(self):
        """Issue new codes only for unbound profiles. Print them in the local console."""
        codes = []
        with self.connect() as db:
            for e in db.execute('SELECT * FROM employees WHERE telegram_id IS NULL ORDER BY id').fetchall():
                code = secrets.token_urlsafe(12)
                db.execute('UPDATE employees SET enrollment_hash=? WHERE id=?',
                           (sha256(code.encode()).hexdigest(), e['id']))
                codes.append((e['name'], e['role'], code))
        return codes

    def enroll(self, user, code):
        with self.connect() as db:
            if db.execute('SELECT 1 FROM employees WHERE telegram_id=?', (user,)).fetchone():
                raise UserError('Ваш аккаунт уже подключён.')
            e = db.execute('SELECT * FROM employees WHERE enrollment_hash=? AND telegram_id IS NULL',
                           (sha256(code.strip().encode()).hexdigest(),)).fetchone()
            if not e:
                raise UserError('Код не найден или уже использован. Получите код у организатора.')
            db.execute('UPDATE employees SET telegram_id=?, enrollment_hash=NULL WHERE id=?',
                       (user, e['id']))
        return self.profile(user)

    def profile(self, user):
        with self.connect() as db:
            e = db.execute('SELECT * FROM employees WHERE telegram_id=?', (user,)).fetchone()
        return dict(e) if e else None

    def require_profile(self, user):
        profile = self.profile(user)
        if not profile:
            raise UserError('Сначала подключите профиль одноразовым кодом организатора.')
        return profile

    def employees(self):
        with self.connect() as db:
            return [dict(e) for e in db.execute('SELECT id,name,role,telegram_id FROM employees ORDER BY name')]

    def peers(self, target):
        with self.connect() as db:
            rows = db.execute('''
                SELECT e.id,e.name,e.telegram_id,p.name AS project,p.id AS project_id
                FROM memberships own JOIN memberships other ON own.project=other.project
                JOIN employees e ON e.id=other.employee JOIN projects p ON p.id=own.project
                WHERE own.employee=? AND other.employee<>? AND e.role='employee'
                ORDER BY e.name,p.name''', (target, target)).fetchall()
        people = {}
        for row in rows:
            peer = people.setdefault(row['id'], {'id': row['id'], 'name': row['name'],
                                                 'telegram_id': row['telegram_id'], 'projects': []})
            peer['projects'].append(row['project'])
        return sorted(people.values(), key=lambda p: (-len(p['projects']), p['name']))

    def candidates(self, target):
        return [e for e in self.employees() if e['id'] != target and e['role'] == 'employee']

    def create(self, user, target=None, selected=None):
        profile = self.require_profile(user)
        target = target or profile['id']
        if profile['role'] != 'hr' and target != profile['id']:
            raise UserError('Вы можете запрашивать обратную связь только о себе.')
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM employees WHERE id=? AND role='employee'", (target,)).fetchone():
                raise UserError('Для опроса выберите профиль сотрудника.')
        candidates = self.candidates(target)
        selected = set(selected or [])
        if not selected.issubset({e['id'] for e in candidates}):
            raise UserError('В списке есть недоступный сотрудник или сам получатель обратной связи.')
        token = secrets.token_urlsafe(18)
        with self.connect() as db:
            db.execute('INSERT INTO campaigns(id,requester,target,questionnaire) VALUES(?,?,?,?)',
                       (token, user, target, json.dumps(QUESTIONNAIRE, ensure_ascii=False)))
            db.executemany('INSERT INTO invitations(campaign,employee) VALUES(?,?)',
                           [(token, e['id']) for e in candidates if e['id'] in selected])
        return token, candidates

    def toggle_colleague(self, user, token, employee):
        c = self.controlled(user, token)
        if c['state'] != 'draft':
            raise UserError('После запуска изменить список коллег нельзя. Создайте новый опрос.')
        if employee not in {e['id'] for e in self.candidates(c['target'])}:
            raise UserError('Этого сотрудника нельзя выбрать в данном опросе.')
        with self.connect() as db:
            if db.execute('SELECT 1 FROM invitations WHERE campaign=? AND employee=?', (token, employee)).fetchone():
                db.execute('DELETE FROM invitations WHERE campaign=? AND employee=?', (token, employee))
            else:
                db.execute('INSERT INTO invitations(campaign,employee) VALUES(?,?)', (token, employee))

    def campaign(self, token):
        with self.connect() as db:
            c = db.execute('''SELECT c.*,e.name AS subject FROM campaigns c
                              JOIN employees e ON c.target=e.id WHERE c.id=?''', (token,)).fetchone()
        if not c:
            raise UserError('Опрос не найден.')
        result = dict(c)
        result['questions'] = json.loads(result.pop('questionnaire'))
        return result

    def authorized(self, user, token):
        p, c = self.require_profile(user), self.campaign(token)
        if p['role'] != 'hr' and p['id'] != c['target']:
            raise UserError('Отчёт доступен сотруднику, о котором собрана обратная связь, и HR.')
        return c

    def controlled(self, user, token):
        p, c = self.require_profile(user), self.campaign(token)
        if p['role'] != 'hr' and user != c['requester']:
            raise UserError('Запустить или отменить этот опрос может только его создатель или HR.')
        return c

    def invitations(self, token):
        with self.connect() as db:
            return [dict(r) for r in db.execute('''SELECT i.*,e.name,e.telegram_id FROM invitations i
                JOIN employees e ON e.id=i.employee WHERE campaign=? ORDER BY e.name''', (token,))]

    def mark_delivery(self, token, employee, status, now=None):
        with self.connect() as db:
            db.execute('''UPDATE invitations SET delivery=?, attempts=attempts+1,last_attempt=?
                          WHERE campaign=? AND employee=?''',
                       (status, time.time() if now is None else now, token, employee))

    def due_invitations(self, token=None, user=None, now=None):
        """Durable queue: only registered, eligible colleagues in open non-demo surveys."""
        now = time.time() if now is None else now
        with self.connect() as db:
            rows = db.execute('''SELECT i.*,e.telegram_id,c.target,t.name AS subject
                FROM invitations i JOIN campaigns c ON c.id=i.campaign
                JOIN employees e ON e.id=i.employee JOIN employees t ON t.id=c.target
                WHERE c.state='open' AND c.demo=0 AND e.telegram_id IS NOT NULL
                AND i.delivery IN ('pending','waiting','failed') AND i.attempts<5
                AND NOT EXISTS(SELECT 1 FROM responses r
                               WHERE r.campaign=i.campaign AND r.employee=i.employee)
                ORDER BY c.rowid,e.id''').fetchall()
        result = []
        for row in rows:
            if token is not None and row['campaign'] != token:
                continue
            if user is not None and row['telegram_id'] != user:
                continue
            delay = min(600, 30 * 2 ** max(0, row['attempts'] - 1))
            if row['attempts'] and now - row['last_attempt'] < delay:
                continue
            result.append(dict(row))
        return result

    def delivery_stats(self, user, token):
        self.authorized(user, token)
        invitations = self.invitations(token)
        return {
            'sent': sum(i['delivery'] == 'sent' for i in invitations),
            'waiting': sum(i['telegram_id'] is None and i['delivery'] != 'sent' for i in invitations),
            'retrying': sum(i['telegram_id'] is not None and i['delivery'] != 'sent' and i['attempts'] < 5
                            for i in invitations),
            'failed': sum(i['delivery'] != 'sent' and i['attempts'] >= 5 for i in invitations),
        }

    def activate(self, user, token):
        c = self.controlled(user, token)
        if c['state'] != 'draft':
            raise UserError('Этот опрос уже запущен или отменён.')
        if len(self.invitations(token)) < MIN_RESPONSES:
            raise UserError(f'Выберите минимум {MIN_RESPONSES} коллег для сбора обратной связи.')
        with self.connect() as db:
            db.execute("UPDATE campaigns SET state='open' WHERE id=?", (token,))

    def discard(self, user, token):
        c = self.controlled(user, token)
        if c['state'] != 'draft':
            raise UserError('Можно отменить только ещё не запущенный опрос.')
        with self.connect() as db:
            db.execute("UPDATE campaigns SET state='cancelled' WHERE id=?", (token,))

    def eligible(self, user, token):
        p, c = self.require_profile(user), self.campaign(token)
        if c['state'] != 'open' or c['demo']:
            raise UserError('Этот опрос сейчас недоступен для ответов.')
        if p['id'] == c['target']:
            raise UserError('Нельзя оставить отзыв о себе.')
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM invitations WHERE campaign=? AND employee=?',
                              (token, p['id'])).fetchone():
                raise UserError('Вы не входите в список коллег для этого опроса.')
            if db.execute('SELECT 1 FROM responses WHERE campaign=? AND employee=?',
                          (token, p['id'])).fetchone():
                raise UserError('Вы уже оставили отзыв. Спасибо!')
        return c

    def begin(self, user, token):
        c = self.eligible(user, token)
        with self.connect() as db:
            if db.execute('SELECT 1 FROM sessions WHERE user=?', (user,)).fetchone():
                raise UserError('У вас есть незавершённый опрос. Продолжите его или нажмите «Отменить ответ».')
            db.execute('INSERT INTO sessions VALUES(?,?,?)', (user, token, '[]'))
        return c

    def session(self, user):
        with self.connect() as db:
            s = db.execute('SELECT * FROM sessions WHERE user=?', (user,)).fetchone()
        return dict(s) if s else None

    def cancel(self, user):
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE user=?', (user,))

    def answer(self, user, text, message_id=None):
        profile = self.require_profile(user)
        with self.connect() as db:
            if message_id is not None:
                receipt = db.execute('SELECT step FROM receipts WHERE user=? AND message_id=?',
                                     (user, message_id)).fetchone()
                if receipt:
                    return receipt['step']
            session = db.execute('SELECT * FROM sessions WHERE user=?', (user,)).fetchone()
            if not session:
                return None
            if not 2 <= len(text.strip()) <= 1200:
                raise UserError('Ответ должен содержать от 2 до 1200 символов.')
            answers = json.loads(session['answers']) + [text.strip()]
            step = len(answers)
            question_count = len(self.campaign(session['campaign'])['questions'])
            if step == question_count:
                db.execute('INSERT INTO responses VALUES(?,?,?)',
                           (session['campaign'], profile['id'], json.dumps(answers, ensure_ascii=False)))
                db.execute('DELETE FROM sessions WHERE user=?', (user,))
            else:
                db.execute('UPDATE sessions SET answers=? WHERE user=?',
                           (json.dumps(answers, ensure_ascii=False), user))
            if message_id is not None:
                db.execute('INSERT INTO receipts VALUES(?,?,?)', (user, message_id, step))
        return step

    def visible(self, user):
        p = self.require_profile(user)
        with self.connect() as db:
            if p['role'] == 'hr':
                ids = [r['id'] for r in db.execute('SELECT id FROM campaigns ORDER BY rowid DESC LIMIT 20')]
            else:
                ids = [r['id'] for r in db.execute(
                    'SELECT id FROM campaigns WHERE target=? ORDER BY rowid DESC LIMIT 20', (p['id'],))]
        return [self.campaign(i) for i in ids]

    def available(self, user):
        p = self.require_profile(user)
        with self.connect() as db:
            ids = [r['id'] for r in db.execute('''SELECT c.id FROM campaigns c
                JOIN invitations i ON c.id=i.campaign
                WHERE i.employee=? AND c.state='open' AND c.demo=0
                AND NOT EXISTS(SELECT 1 FROM responses r WHERE r.campaign=c.id AND r.employee=?)
                ORDER BY c.rowid DESC LIMIT 20''', (p['id'], p['id']))]
        return [self.campaign(i) for i in ids]

    def responses(self, token):
        with self.connect() as db:
            return [json.loads(r['answers']) for r in db.execute(
                'SELECT answers FROM responses WHERE campaign=? ORDER BY rowid', (token,))]

    def stats(self, user, token):
        c = self.authorized(user, token)
        with self.connect() as db:
            invited = db.execute('SELECT COUNT(*) FROM invitations WHERE campaign=?', (token,)).fetchone()[0]
            count = db.execute('SELECT COUNT(*) FROM responses WHERE campaign=?', (token,)).fetchone()[0]
        return c, invited, count

    def report_data(self, user, token):
        c = self.authorized(user, token)
        if c['state'] != 'open':
            raise UserError('Отчёт доступен только для запущенного опроса.')
        answers = self.responses(token)
        if len(answers) < MIN_RESPONSES:
            raise UserError(f'Получено отзывов: {len(answers)}. Для отчёта нужно минимум {MIN_RESPONSES}.')
        return c, answers

    def cached_report(self, token, count):
        with self.connect() as db:
            row = db.execute('SELECT report FROM reports WHERE campaign=? AND count=?', (token, count)).fetchone()
        return json.loads(row['report']) if row else None

    def save_report(self, token, count, report):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO reports VALUES(?,?,?)',
                       (token, count, json.dumps(report, ensure_ascii=False)))

    def demo(self, user, answers):
        p = self.require_profile(user)
        if p['role'] == 'hr':
            target = next(e['id'] for e in self.employees() if e['role'] == 'employee')
        else:
            target = p['id']
        peers = self.candidates(target)[:len(answers)]
        token, _ = self.create(user, target, selected=[p['id'] for p in peers])
        if len(answers) < MIN_RESPONSES or len(answers) > len(peers):
            raise ValueError('Demo needs enough distinct invited employees')
        with self.connect() as db:
            db.execute("UPDATE campaigns SET demo=1,state='open' WHERE id=?", (token,))
            for peer, answer in zip(peers, answers):
                db.execute('INSERT INTO responses VALUES(?,?,?)',
                           (token, peer['id'], json.dumps(answer, ensure_ascii=False)))
        return token

    def metadata(self, key, value=None):
        with self.connect() as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', (key, str(value)))
            row = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return row['value'] if row else None
