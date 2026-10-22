import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from helpers import UserError
from trck import Tracker
from store import Store
from bot import FIXTURE

class TrackerTests(unittest.TestCase):
    def test_roster_pagination_and_employee_filter(self):
        pages = [{'content': [{'id': 1, 'firstName': 'Nina', 'lastName': 'Kniazeva', 'email': 'nina@example.org'}], 'number': 0, 'totalElements': 2, 'last': False},
                 {'content': [{'id': 2, 'firstName': 'Daria', 'lastName': 'Nemirich', 'email': 'daria@example.org'}], 'number': 1, 'totalElements': 2, 'last': True}]
        calls = []
        def fetch(url, **kwargs):
            calls.append(url)
            return pages[len(calls)-1]
        client = Tracker('.', fetch)
        with patch.object(client, 'headers', return_value={}):
            self.assertEqual(len(client.employees()), 2)
        self.assertTrue(all('userType=EMPLOYEE' in url and 'isActive=true' in url for url in calls))
        pages[1]['content'] = []
        calls.clear()
        with patch.object(client, 'headers', return_value={}), self.assertRaises(UserError):
            client.employees()

    def test_jira_service_accounts_cannot_create_employees_and_binding_id_is_preserved(self):
        class Matcher:
            jira_url='https://jira.example.org'
            jira_headers={}
            def fetch(self, url, **kwargs):
                return [{'active': True, 'name': 'nina', 'key': 'person1', 'emailAddress': 'Nina@example.org'},
                        {'active': True, 'name': 'assistant', 'key': 'app1', 'emailAddress': 'app@example.org'}]
        previous = {'employees': [{'id': 'existing', 'name': 'Nina', 'role': 'employee', 'jira_key': 'person1', 'jira_user': 'nina'}], 'projects': [{'id':'TEST', 'spaces':[]}]}
        client = Tracker('.')
        with patch.object(client, 'employees', return_value=[{'id':1, 'firstName':'Nina', 'lastName':'Kniazeva', 'email':'nina@example.org'}]):
            directory = client.directory(Matcher(), previous)
        self.assertEqual([e['id'] for e in directory['employees']], ['existing'])
        self.assertEqual(directory['roster_source'], 'trck')

    def test_ambiguous_jira_email_keeps_employee_without_guessed_account(self):
        class Matcher:
            jira_url='https://jira.example.org'
            jira_headers={}
            def fetch(self, url, **kwargs):
                return [{'active': True, 'name': name, 'key': name, 'emailAddress': 'nina@example.org'}
                        for name in ('nina', 'integration')]
        client = Tracker('.')
        with patch.object(client, 'employees', return_value=[{'id':1, 'firstName':'Nina', 'lastName':'Kniazeva', 'email':'nina@example.org'}]):
            directory = client.directory(Matcher(), {'employees':[], 'projects':[{'id':'TEST', 'spaces':[]}]})
        self.assertEqual(len(directory['employees']), 1)
        self.assertIsNone(directory['employees'][0]['jira_user'])

    def test_sync_hides_removed_accounts_in_old_drafts_without_deleting_history_or_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'test.sqlite3', FIXTURE)
            with store.connect() as db:
                db.execute("UPDATE employees SET telegram_id=123 WHERE id='e01'")
            token, people = store.create(123)
            excluded = people[0]['id']
            with store.connect() as db:
                db.execute('INSERT INTO responses(campaign,employee,answers) VALUES(?,?,?)', (token, excluded, '{}'))
            directory = {'roster_source':'trck', 'employees':[e for e in store.employees() if e['id'] != excluded]}
            store.sync_directory(directory)
            self.assertEqual(store.require_profile(123)['id'], 'e01')
            self.assertNotIn(excluded, {e['id'] for e in store.campaign_candidates(token)})
            self.assertNotIn(excluded, {e['employee'] for e in store.invitations(token)})
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM responses').fetchone()[0], 1)
            with self.assertRaises(UserError):
                store.add_colleague(123, token, excluded)
