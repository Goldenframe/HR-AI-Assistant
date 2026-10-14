"""Isolated two-participant trial, with one Telegram owner playing both roles."""
import json
import secrets
import time
from helpers import UserError
from survey import QUESTIONNAIRE, RATINGS

class Trial:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS trial_runs(id TEXT PRIMARY KEY,owner INTEGER NOT NULL,
                    subject TEXT NOT NULL,questions TEXT NOT NULL,report TEXT,delivered INTEGER DEFAULT 0,
                    attempts INTEGER DEFAULT 0,next_attempt REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS trial_sessions(user INTEGER PRIMARY KEY,run TEXT NOT NULL,
                    slot INTEGER NOT NULL,answers TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS trial_answers(run TEXT,slot INTEGER,answers TEXT NOT NULL,
                    PRIMARY KEY(run,slot));
                CREATE TABLE IF NOT EXISTS trial_receipts(user INTEGER,message_id INTEGER,PRIMARY KEY(user,message_id));
            """)

            if 'last_error' not in {r['name'] for r in db.execute('PRAGMA table_info(trial_runs)')}:
                db.execute('ALTER TABLE trial_runs ADD COLUMN last_error TEXT')
            if 'edit_index' not in {r['name'] for r in db.execute('PRAGMA table_info(trial_sessions)')}:
                db.execute('ALTER TABLE trial_sessions ADD COLUMN edit_index INTEGER')

    def create(self, owner, subject='Тестовый сотрудник — Алексей Волков'):
        self.store.require_profile(owner)
        token = secrets.token_urlsafe(16)
        with self.store.connect() as db:
            db.execute('INSERT INTO trial_runs(id,owner,subject,questions) VALUES(?,?,?,?)',
                       (token,owner,subject,json.dumps(QUESTIONNAIRE,ensure_ascii=False)))
        return token

    def run(self, user, token):
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM trial_runs WHERE id=? AND owner=?',(token,user)).fetchone()
        if not row:
            raise UserError('Этот тест доступен только его участнику.')
        result=dict(row);result['questions']=json.loads(result['questions']);return result

    def session(self, user):
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM trial_sessions WHERE user=?',(user,)).fetchone()
        return dict(row) if row else None

    def begin(self,user,token,slot):
        run=self.run(user,token)
        if slot not in (1,2):raise UserError('Неизвестный участник теста.')
        with self.store.connect() as db:
            if db.execute('SELECT 1 FROM trial_answers WHERE run=? AND slot=?',(token,slot)).fetchone():
                raise UserError('Этот тестовый отзыв уже сохранён.')
            existing=self.session(user)
            if existing:
                if existing['run']==token and existing['slot']==slot:return run
                raise UserError('Сначала завершите текущий отзыв или отмените ответ.')
            if self.store.session(user):raise UserError('Сначала завершите текущий рабочий отзыв.')
            db.execute('INSERT INTO trial_sessions(user,run,slot,answers) VALUES(?,?,?,?)',(user,token,slot,'[]'))
        return run

    def cancel(self,user):
        with self.store.connect() as db:db.execute('DELETE FROM trial_sessions WHERE user=?',(user,))

    def answer(self,user,text,message_id=None,review=False):
        session=self.session(user)
        if not session:return None
        run=self.run(user,session['run']);answers=json.loads(session['answers'])
        with self.store.connect() as db:
            if message_id is not None and db.execute('SELECT 1 FROM trial_receipts WHERE user=? AND message_id=?',(user,message_id)).fetchone():
                return None
            if not 2<=len(text.strip())<=1200:raise UserError('Ответ должен содержать от 2 до 1200 символов.')
            index=session['edit_index'] if session['edit_index'] is not None else len(answers)
            if index>=len(run['questions']):raise UserError('Анкета заполнена. Проверьте ответы и нажмите «Отправить отзыв».')
            if run['questions'][index].get('type')=='rating' and text.strip() not in RATINGS:
                raise UserError('Выберите оценку кнопкой.')
            if session['edit_index'] is not None:answers[index]=text.strip()
            else:answers.append(text.strip())
            if len(answers)==len(run['questions']) and not review:
                db.execute('INSERT INTO trial_answers VALUES(?,?,?)',(session['run'],session['slot'],json.dumps(answers,ensure_ascii=False)))
                db.execute('DELETE FROM trial_sessions WHERE user=?',(user,))
            else:db.execute('UPDATE trial_sessions SET answers=?,edit_index=NULL WHERE user=?',(json.dumps(answers,ensure_ascii=False),user))
            if message_id is not None:db.execute('INSERT INTO trial_receipts VALUES(?,?)',(user,message_id))
        return session['run'],session['slot'],len(answers)

    def answers(self,token):
        with self.store.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT answers FROM trial_answers WHERE run=? ORDER BY slot',(token,))]

    def ready(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM trial_runs t WHERE delivered=0 AND next_attempt<=? AND (SELECT COUNT(*) FROM trial_answers a WHERE a.run=t.id)=2',(time.time(),))]

    def attempted(self,token):
        with self.store.connect() as db:
            attempts=db.execute('SELECT attempts FROM trial_runs WHERE id=?',(token,)).fetchone()[0]
            delay=min(300,60*(2**min(attempts,3)))
            db.execute('UPDATE trial_runs SET attempts=attempts+1,next_attempt=? WHERE id=?',(time.time()+delay,token))

    def record_error(self,token,message):
        with self.store.connect() as db:
            previous=db.execute('SELECT last_error FROM trial_runs WHERE id=?',(token,)).fetchone()[0]
            db.execute('UPDATE trial_runs SET last_error=? WHERE id=?',(message,token))
        return previous!=message

    def save_report(self,token,report):
        with self.store.connect() as db:db.execute('UPDATE trial_runs SET report=?,last_error=NULL WHERE id=?',(json.dumps(report,ensure_ascii=False),token))

    def delivered(self,token):
        with self.store.connect() as db:db.execute('UPDATE trial_runs SET delivered=1 WHERE id=?',(token,))

    def retry(self,user,token):
        self.run(user,token)
        with self.store.connect() as db:db.execute('UPDATE trial_runs SET attempts=0,next_attempt=0 WHERE id=? AND delivered=0',(token,))
