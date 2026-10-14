import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

from atlassian import AtlassianMatcher, load_directory
from bot import FIXTURE
from helpers import UserError, JiraAccountError
from store import Store


class AtlassianTests(unittest.TestCase):
    def setUp(self):
        self.directory = {'employees': [
            {'id': 'e01', 'name': 'Первый', 'role': 'employee', 'jira_user': 'one', 'confluence_user': 'conf-one'},
            {'id': 'e02', 'name': 'Второй', 'role': 'employee', 'jira_user': 'two', 'confluence_user': 'conf-two'},
            {'id': 'hr01', 'name': 'HR', 'role': 'hr'}],
            'projects': [{'id': 'ONE', 'name': 'Первый проект', 'spaces': ['DOC']} ]}
        self.calls = []

    def matcher(self, fetch):
        return AtlassianMatcher(self.directory, 'http://localhost:111', 'fake-jira-key',
                                'http://localhost:222', 'fake-conf-key', fetch=fetch)

    def test_paginated_jira_historical_assignments_and_logged_work(self):
        def fetch(url, **kwargs):
            self.calls.append((url, kwargs))
            query = parse_qs(urlparse(url).query)
            self.assertIn('assignee WAS "one"', query['jql'][0])
            self.assertIn('reporter = "one"', query['jql'][0])
            self.assertIn('worklogAuthor = "one"', query['jql'][0])
            self.assertNotIn('watcher', query['jql'][0])
            return {'total': 2, 'issues': [{'fields': {'project': {'key': 'ONE'}}}]}
        self.assertEqual(self.matcher(fetch).jira_projects('one'), {'ONE'})
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(parse_qs(urlparse(self.calls[1][0]).query)['startAt'], ['1'])
        self.assertEqual(self.calls[0][1]['headers']['Authorization'], 'Bearer fake-jira-key')

    def test_confluence_maps_space_to_project_and_never_follows_external_next_link(self):
        def fetch(url, **kwargs):
            self.calls.append(url)
            query = parse_qs(urlparse(url).query)
            self.assertIn('contributor = "conf-one"', query['cql'][0])
            self.assertIn('space in ("DOC")', query['cql'][0])
            return {'results': [{'space': {'key': 'DOC'}}],
                    '_links': {'next': 'https://untrusted.example/next'} if len(self.calls) == 1 else {}}
        self.assertEqual(self.matcher(fetch).confluence_projects('conf-one'), {'ONE'})
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(url.startswith('http://localhost:222/') for url in self.calls))

    def test_cross_source_project_matching_and_snapshot_is_stable(self):
        def fetch(url, **kwargs):
            query = parse_qs(urlparse(url).query)
            if 'jql' in query:
                return {'issues': [{'fields': {'project': {'key': 'ONE'}}}] if '"one"' in query['jql'][0] else [],
                        'total': 1 if '"one"' in query['jql'][0] else 0}
            return {'results': [{'space': {'key': 'DOC'}}] if '"conf-two"' in query['cql'][0] else [], '_links': {}}
        snapshot = self.matcher(fetch).collect()
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'store.sqlite3', FIXTURE)
            with store.connect() as db:
                db.execute("UPDATE employees SET telegram_id=100 WHERE id='e01'")
            store.sync_participation(snapshot)
            token, candidates = store.create(100)
            self.assertEqual([c['id'] for c in candidates], ['e02'])
            reason = candidates[0]['reasons'][0]
            self.assertEqual(reason['target_sources'], ['Jira'])
            self.assertEqual(reason['peer_sources'], ['Confluence'])
            store.sync_participation({'projects': snapshot['projects'], 'memberships': []})
            self.assertEqual(store.candidates('e01'), [])
            self.assertEqual(store.campaign_candidates(token), candidates)
            self.assertEqual(len(store.invitations(token)), 1)

    def test_access_failure_does_not_replace_membership_snapshot(self):
        def fetch(url, **kwargs):
            raise UserError('Сервис вернул HTTP 403.')
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'store.sqlite3', FIXTURE)
            before = store.candidates('e01')
            with self.assertRaises(UserError):
                store.sync_participation(self.matcher(fetch).collect())
            self.assertEqual(store.candidates('e01'), before)

    def test_truncated_results_fail_instead_of_partial_success(self):
        matcher = self.matcher(lambda url, **kwargs: {'issues': [], 'total': 100})
        with self.assertRaises(UserError):
            matcher.jira_projects('one')
        matcher = self.matcher(lambda url, **kwargs: {'results': [], '_links': {'next': 'next'}})
        with self.assertRaises(UserError):
            matcher.confluence_projects('one')

    def test_directory_rejects_duplicate_identities_and_ambiguous_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'directory.json'
            path.write_text(json.dumps(self.directory), encoding='utf-8')
            self.assertEqual(load_directory(path)['projects'][0]['members'], [])
            self.directory['employees'][1]['jira_user'] = 'one'
            path.write_text(json.dumps(self.directory), encoding='utf-8')
            with self.assertRaises(UserError):
                load_directory(path)

            self.directory['employees'][1]['jira_user'] = 'two'
            self.directory['projects'].append({'id': 'TWO', 'spaces': ['DOC']})
            path.write_text(json.dumps(self.directory), encoding='utf-8')
            with self.assertRaises(UserError):
                load_directory(path)

    def test_import_users_projects_and_exact_space_keys_without_manual_roster(self):
        calls = []
        def fetch(url, **kwargs):
            calls.append(url)
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            if parsed.path.endswith('/mypermissions'):
                return {'permissions': {'USER_PICKER': {'havePermission': True}}}
            if parsed.path.endswith('/user/search'):
                if query['startAt'] == ['0']:
                    return [{'key': 'stable-one', 'name': 'one', 'displayName': 'Первый', 'active': True},
                            {'key': 'stable-two', 'name': 'two', 'displayName': 'Второй', 'active': True},
                            {'key': 'inactive', 'name': 'old', 'active': False}]
                return []
            if parsed.path.endswith('/project'):
                return [{'key': 'ONE', 'name': 'Один'}, {'key': 'TWO', 'name': 'Название совпадает'}]
            if parsed.path.endswith('/space'):
                return {'results': [{'key': 'ONE', 'name': 'Один'},
                                    {'key': 'OTHER', 'name': 'Название совпадает'}], '_links': {}}
            if parsed.path.endswith('/search'):
                return {'results': [], 'totalSize': 0}
            if parsed.path.endswith('/user'):
                if query['username'] == ['two']:
                    raise UserError('Сервис вернул HTTP 404. Проверьте доступ и ключ.')
                return {'username': 'one'}
            raise AssertionError(url)
        imported = self.matcher(fetch).import_directory()
        self.assertEqual(len(imported['employees']), 3)  # two active employees + separate HR
        self.assertEqual(imported['employees'][0]['confluence_user'], 'one')
        self.assertIsNone(imported['employees'][1]['confluence_user'])
        self.assertEqual(imported['projects'][0]['spaces'], ['ONE'])
        self.assertEqual(imported['projects'][1]['spaces'], [])
        self.assertEqual(imported['unmapped_spaces'][0]['key'], 'OTHER')
        old_id = imported['employees'][0]['id']
        self.assertEqual(self.matcher(fetch).import_directory(imported)['employees'][0]['id'], old_id)
        self.assertTrue(all('/user/search?' not in url or 'includeInactive=false' in url for url in calls))

    def test_import_requires_directory_permission_and_rejects_repeated_page(self):
        with self.assertRaises(UserError):
            self.matcher(lambda url, **kwargs: {'permissions': {}}).import_directory()
        def fetch(url, **kwargs):
            if '/mypermissions?' in url:
                return {'permissions': {'USER_PICKER': {'havePermission': True}}}
            return [{'key': 'same', 'name': 'one', 'active': True}]
        with self.assertRaises(UserError) as error:
            self.matcher(fetch).import_directory()
        self.assertIn('повторяет', str(error.exception))

    def test_import_links_unique_email_across_different_usernames_and_total_pagination(self):
        seen_starts = []
        def fetch(url, **kwargs):
            parsed, query = urlparse(url), parse_qs(urlparse(url).query)
            if parsed.path.endswith('/mypermissions'):
                return {'permissions': {'USER_PICKER': {'havePermission': True}}}
            if parsed.path.endswith('/user/search'):
                return [] if query['startAt'] != ['0'] else [
                    {'key': 'key-one', 'name': 'jira-one', 'emailAddress': 'ONE@example.test', 'active': True}]
            if parsed.path.endswith('/project'):
                return [{'key': 'ONE'}]
            if parsed.path.endswith('/space'):
                return {'results': [], '_links': {}}
            if parsed.path.endswith('/search'):
                start = int(query['start'][0])
                seen_starts.append(start)
                return {'results': [{'user': {'username': 'conf-one' if start == 0 else 'deleted'}}],
                        'totalSize': 2, '_links': {}}
            if parsed.path.endswith('/user'):
                if query['username'] != ['conf-one']:
                    raise UserError('Сервис вернул HTTP 404. Проверьте доступ и ключ.')
                return {'username': 'conf-one', 'email': 'one@example.test'}
            raise AssertionError(url)
        result = self.matcher(fetch).import_directory()
        self.assertEqual(seen_starts, [0, 1])
        self.assertEqual(result['employees'][0]['confluence_user'], 'conf-one')
        self.assertNotIn('email', result['employees'][0])

    def test_legacy_jql_account_is_matched_by_fresh_project_activity_and_stable_key(self):
        self.directory['employees'][0].update(jira_key='stable-one', project_keys=['ONE'])
        def fetch(url, **kwargs):
            query = parse_qs(urlparse(url).query)
            if 'jql' in query:
                if 'assignee WAS "one"' in query['jql'][0]:
                    raise UserError('Сервис вернул HTTP 400. Проверьте доступ и ключ.')
                if 'assignee WAS' in query['jql'][0]:
                    return {'issues': [], 'total': 0}
                return {'issues': [{'fields': {'assignee': {'key': 'stable-one', 'name': 'one', 'active': True},
                                               'reporter': None, 'worklog': {'worklogs': [], 'total': 0}}}], 'total': 1}
            return {'results': [], '_links': {}}
        snapshot = self.matcher(fetch).collect()
        self.assertEqual(snapshot['memberships'], [{'employee': 'e01', 'project': 'ONE', 'sources': ['Jira']}])

    def test_unsearchable_account_without_known_projects_does_not_block_other_people(self):
        def fetch(url, **kwargs):
            query = parse_qs(urlparse(url).query)
            if 'jql' in query:
                if 'assignee WAS "one"' in query['jql'][0]:
                    raise JiraAccountError('Jira: аккаунт сотрудника не распознан в поиске проектов.')
                return {'issues': [{'fields': {'project': {'key': 'ONE'}}}], 'total': 1}
            return {'results': [{'space': {'key': 'DOC'}}] if '"conf-one"' in query['cql'][0] else [], '_links': {}}
        snapshot = self.matcher(fetch).collect()
        self.assertEqual(snapshot['memberships'], [
            {'employee':'e01', 'project':'ONE', 'sources':['Confluence']},
            {'employee':'e02', 'project':'ONE', 'sources':['Jira']}])
        self.assertEqual(snapshot['warnings'], [{'employee':'e01', 'service':'Jira', 'reason':'account_not_searchable'}])
        self.assertEqual(len(self.directory['employees']), 3)

    def test_unrelated_invalid_jql_still_fails_instead_of_empty_match(self):
        def fetch(url, **kwargs):
            raise UserError('Jira: HTTP 400. Запрос отклонён.')
        with self.assertRaises(UserError):
            self.matcher(fetch).collect()


if __name__ == '__main__':
    unittest.main()
