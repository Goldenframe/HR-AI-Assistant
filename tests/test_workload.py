import json
from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from ai import generate_report, REPORT_VERSION, format_report
from bot import Bot
from workload import jira_workload, unavailable, workload_text
from survey import QUESTIONNAIRE, SKIP
from helpers import UserError

class WorkloadTests(unittest.TestCase):
    def matcher(self, fetch):
        return SimpleNamespace(directory={'employees':[{'id':'e1','jira_user':'user'}]},
                               jira_url='http://local',jira_headers={},fetch=fetch)
    def issue(self, key, estimate=None, due=None, done=False):
        return {'key':key,'fields':{'timeestimate':estimate,'duedate':due,
          'status':{'statusCategory':{'key':'done' if done else 'new'}}}}
    def test_paginated_workload_uses_remaining_estimates_and_dates_not_historical_activity(self):
        calls=[]
        pages=[[self.issue('A',3600,'2026-10-13'),self.issue('B',0,'2026-10-28')],
               [self.issue('C',None,None),self.issue('D',7200,'2026-11-01')]]
        def fetch(url,**kwargs):
            query=parse_qs(urlparse(url).query);calls.append(query)
            self.assertIn('assignee = "user"',query['jql'][0])
            self.assertIn('statusCategory != Done',query['jql'][0])
            self.assertNotIn('WAS',query['jql'][0]);self.assertLessEqual(kwargs['timeout'],8)
            return {'total':4,'issues':pages[len(calls)-1]}
        data=jira_workload(self.matcher(fetch),'e1',date(2026,10,14))
        self.assertEqual(data['open_tasks'],4);self.assertEqual(data['remaining_seconds'],10800)
        self.assertEqual(data['estimated_tasks'],3);self.assertEqual(data['unestimated_tasks'],1)
        self.assertEqual(data['overdue_tasks'],1);self.assertEqual(data['due_next_14_days'],1)
        self.assertEqual(calls[1]['startAt'],['2']);self.assertFalse(data['capacity_known'])
    def test_missing_estimates_are_not_reported_as_zero_hours_and_zero_tasks_not_free_capacity(self):
        def fetch(url,**kwargs):return {'total':1,'issues':[self.issue('A')]}
        text=workload_text(jira_workload(self.matcher(fetch),'e1',date(2026,10,14)))
        self.assertNotIn('0 ч',text);self.assertIn('Без оценки: 1',text)
        empty=jira_workload(self.matcher(lambda *a,**k:{'total':0,'issues':[]}),'e1')
        self.assertIn('не означает отсутствие нагрузки',workload_text(empty))
    def test_no_mapping_or_service_failure_does_not_block_bot(self):
        matcher=self.matcher(lambda *a,**k: (_ for _ in ()).throw(UserError('secret error')))
        self.assertEqual(jira_workload(matcher,'unknown'),unavailable('account_unmapped'))
        bot=Bot.__new__(Bot);bot.matcher=matcher
        self.assertEqual(bot.report_workload('e1'),unavailable())
        self.assertEqual(bot.report_workload('e1',True),unavailable('test_or_not_configured'))
    def test_partial_or_duplicate_pages_are_not_claimed_as_complete(self):
        for page in ({'total':2,'issues':[]},{'total':2,'issues':[self.issue('A')]}):
            with self.assertRaises(UserError):jira_workload(self.matcher(lambda *a,**k:page),'e1')
    def test_workload_is_passed_to_ai_and_rendered_separately_from_feedback(self):
        snapshot={'available':True,'open_tasks':3,'estimated_tasks':0,'unestimated_tasks':3,
                  'due_next_14_days':0,'overdue_tasks':0,'capacity_known':False}
        parsed={'version':REPORT_VERSION,'strengths':[],'development':[],'advice':[],'contradictions':[]}
        with patch('ai.request_json',return_value={'choices':[{'message':{'content':json.dumps(parsed)}}]}) as fetch:
            report=generate_report([[SKIP]*len(QUESTIONNAIRE)],('http://local','fake','fake'),workload=snapshot)
        data=json.loads(fetch.call_args.args[1]['messages'][1]['content'].split('\n/no_think')[0])
        self.assertEqual(data['workload'],snapshot);self.assertEqual(report['_workload'],snapshot)
        text=format_report({'subject':'Тест','demo':False},report,3)
        self.assertIn('открытых задач — 3',text);self.assertIn('свободное время неизвестно',text)
