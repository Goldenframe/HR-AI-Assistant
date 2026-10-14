"""Review and edit complete drafts before they become submitted feedback."""
import json
from helpers import UserError

class AnswerReview:
    def __init__(self,store,trial=None):self.store,self.trial=store,trial

    def context(self,user,token,slot=None):
        session=self.trial.session(user) if slot is not None else self.store.session(user)
        if not session or (session['run'] if slot is not None else session['campaign'])!=token or (slot is not None and session['slot']!=slot):
            raise UserError('Этот черновик уже отправлен или больше не открыт.')
        run=self.trial.run(user,token) if slot is not None else self.store.eligible(user,token)
        answers=json.loads(session['answers'])
        if len(answers)!=len(run['questions']):raise UserError('Сначала ответьте на все вопросы.')
        return session,run,answers

    def edit(self,user,token,index,slot=None):
        session,run,answers=self.context(user,token,slot)
        if not 0<=index<len(answers):raise UserError('Вопрос не найден.')
        table='trial_sessions' if slot is not None else 'sessions'
        with self.store.connect() as db:db.execute(f'UPDATE {table} SET edit_index=? WHERE user=?',(index,user))

    def submit(self,user,token,slot=None):
        session,run,answers=self.context(user,token,slot)
        if session['edit_index'] is not None:raise UserError('Сначала сохраните новый ответ.')
        with self.store.connect() as db:
            if slot is not None:
                db.execute('INSERT INTO trial_answers VALUES(?,?,?)',(token,slot,json.dumps(answers,ensure_ascii=False)))
                db.execute('DELETE FROM trial_sessions WHERE user=?',(user,))
            else:
                employee=self.store.require_profile(user)['id']
                db.execute('INSERT INTO responses VALUES(?,?,?)',(token,employee,json.dumps(answers,ensure_ascii=False)))
                db.execute('DELETE FROM sessions WHERE user=?',(user,))
                db.execute('DELETE FROM reminders WHERE campaign=? AND employee=?',(token,employee))
        return run
