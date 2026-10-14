import json
import queue
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from bot import Bot, FIXTURE
from store import Store
from trial import Trial
from helpers import UserError
from survey import RATINGS, SKIP

class TrialTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'db.sqlite3',FIXTURE)
        with self.store.connect() as db:db.execute("UPDATE employees SET telegram_id=123 WHERE id='e01'")
        self.trial=Trial(self.store);self.token=self.trial.create(123)

    def tearDown(self):self.tmp.cleanup()

    def fill(self,slot):
        run=self.trial.begin(123,self.token,slot)
        for i,q in enumerate(run['questions']):
            self.trial.answer(123,RATINGS[1] if q.get('type')=='rating' else f'Пример участника {slot}: помогает обсуждать решения.',slot*1000+i)

    def test_two_roles_are_separate_durable_and_do_not_change_real_profiles_or_reports(self):
        with self.assertRaises(UserError):self.trial.begin(999,self.token,1)
        self.fill(1)
        self.assertEqual(self.trial.ready(),[])
        self.trial=Trial(Store(self.store.path))
        self.fill(2)
        self.assertEqual(len(self.trial.answers(self.token)),2)
        self.assertEqual(len(self.trial.ready()),1)
        self.assertEqual(self.store.require_profile(123)['id'],'e01')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM responses').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM campaigns').fetchone()[0],0)
        with self.assertRaises(UserError):self.trial.begin(123,self.token,1)
        production,_=self.store.create(123,selected=['e02','e03','e04'])
        self.store.activate(123,production)
        with self.assertRaises(UserError):self.store.report_data(123,production)

    def test_second_completion_queues_real_analysis_and_report_for_owner(self):
        self.fill(1);self.fill(2)
        bot=Bot.__new__(Bot);bot.trial=self.trial;bot.pending=set();bot.reports=queue.Queue();bot.settings=('url','key','model');bot.send=lambda *args:None
        report={'strengths':[],'development':[],'advice':[],'contradictions':[]}
        with patch('bot.generate_report',return_value=report) as analysis:
            bot.dispatch_trial_reports()
            token,user,text=bot.reports.get(timeout=5)
            self.assertEqual(user,123)
            self.assertEqual(token,'trial:'+self.token)
            self.assertIn('Получено отзывов: 2',text)
            self.assertEqual(len(analysis.call_args.args[0]),2)
            self.assertIsNotNone(self.trial.run(123,self.token)['report'])
        self.trial.delivered(self.token)
        self.assertEqual(self.trial.ready(),[])

    def test_inline_skip_in_trial_keeps_role_and_prevents_double_skip(self):
        self.trial.begin(123,self.token,1)
        self.trial.answer(123,RATINGS[0])
        bot=Bot.__new__(Bot);bot.trial=self.trial;bot.store=self.store;bot.match_pending=set()
        sent=[];bot.send=lambda *args:sent.append(args)
        bot.ask_trial(123)
        self.assertEqual(sent[-1][2]['inline_keyboard'][0][0]['text'],'Пропустить комментарий')
        bot.action(123,'skiptrial',self.token+':1:1')
        self.assertEqual(json.loads(self.trial.session(123)['answers']),[RATINGS[0],SKIP])
        with self.assertRaises(UserError):bot.action(123,'skiptrial',self.token+':1:1')
        self.assertEqual(self.trial.session(123)['slot'],1)

    def test_outage_does_not_drop_report_after_three_attempts_and_notification_is_deduplicated(self):
        self.fill(1);self.fill(2)
        for i in range(5):
            self.trial.attempted(self.token)
            with self.store.connect() as db:db.execute('UPDATE trial_runs SET next_attempt=0 WHERE id=?',(self.token,))
        self.assertEqual(len(self.trial.ready()),1)
        self.assertTrue(self.trial.record_error(self.token,'ИИ временно недоступен'))
        self.assertFalse(self.trial.record_error(self.token,'ИИ временно недоступен'))
        with self.store.connect() as db:db.execute('UPDATE trial_runs SET next_attempt=99999999999 WHERE id=?',(self.token,))
        self.assertEqual(self.trial.ready(),[])
        self.trial.retry(123,self.token)
        self.assertEqual(len(self.trial.ready()),1)
