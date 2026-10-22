import json
import tempfile
from pathlib import Path
import unittest
from bot import FIXTURE
from store import Store
from trial import Trial
from review import AnswerReview
from helpers import UserError
from survey import RATINGS, SKIP, question_message

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'review.sqlite3',FIXTURE)
        with self.store.connect() as db:
            db.execute("UPDATE employees SET telegram_id=123 WHERE id='e01'")
            db.execute("UPDATE employees SET telegram_id=124 WHERE id='e02'")
        self.trial=Trial(self.store);self.review=AnswerReview(self.store,self.trial)
    def tearDown(self):self.tmp.cleanup()

    def fill(self,source,user,questions):
        base=source.session(user).get('slot',0)*10000
        for i,q in enumerate(questions):source.answer(user,RATINGS[0] if q.get('type')=='rating' else 'Помогает обсуждать решения.',base+i+1,review=True)

    def test_real_complete_draft_can_be_edited_and_only_submission_is_counted(self):
        token,_=self.store.create(123,selected=['e02','e03','e04']);self.store.activate(123,token)
        run=self.store.begin(124,token);self.fill(self.store,124,run['questions'])
        self.assertEqual(self.store.responses(token),[])
        self.review.edit(124,token,0)
        with self.assertRaises(UserError):self.review.submit(124,token)
        self.store.answer(124,RATINGS[-1],1001,review=True)
        self.review.edit(124,token,1);self.store.answer(124,SKIP,1002,review=True)
        with self.assertRaises(UserError):self.review.submit(123,token)
        self.review.submit(124,token)
        self.assertEqual(self.store.responses(token)[0][:2],[RATINGS[-1],SKIP])
        self.assertIsNone(self.store.session(124))
        with self.assertRaises(UserError):self.review.submit(124,token)

    def test_trial_two_roles_require_submission_and_resume_editing_after_restart(self):
        token=self.trial.create(123);run=self.trial.begin(123,token,1)
        self.fill(self.trial,123,run['questions'])
        self.assertEqual(self.trial.answers(token),[])
        self.review.edit(123,token,1,1)
        reloaded=Trial(Store(self.store.path))
        self.assertEqual(reloaded.session(123)['edit_index'],1)
        reloaded.answer(123,'Новый конкретный пример.',1001,review=True)
        self.review.submit(123,token,1)
        self.assertEqual(self.trial.answers(token)[0][1],'Новый конкретный пример.')
        self.assertEqual(self.trial.ready(),[])
        self.trial.begin(123,token,2);self.fill(self.trial,123,run['questions'])
        with self.assertRaises(UserError):self.review.submit(123,token,1)
        self.assertEqual(self.trial.ready(),[])
        self.review.submit(123,token,2)
        self.assertEqual(len(self.trial.ready()),1)

    def test_comment_message_does_not_repeat_previous_ratings(self):
        text=question_message([{'section':'A','text':'Оценка','type':'rating'}, {'section':'A','text':'Комментарий','type':'comment'}],1,[RATINGS[0]])
        self.assertNotIn('Ваши оценки',text)
        self.assertNotIn(RATINGS[0],text)
