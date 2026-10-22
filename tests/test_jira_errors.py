import io
import unittest
import urllib.error
from unittest.mock import patch
from helpers import request_json, UserError, JiraAccountError

class JiraErrorsTests(unittest.TestCase):
    def test_only_specific_account_validation_is_recoverable(self):
        for body, expected in [(b'{"errorMessages":["The value \'missing\' does not exist for the field \'assignee\'."]}', JiraAccountError),
                               (b'{"errorMessages":["Invalid JQL syntax"]}', UserError)]:
            error = urllib.error.HTTPError('https://jira.example/rest/api/2/search',400,'Bad Request',{},io.BytesIO(body))
            with patch('urllib.request.urlopen', side_effect=error):
                with self.assertRaises(UserError) as caught:
                    request_json('https://jira.example/rest/api/2/search')
            self.assertIs(type(caught.exception), expected)
            self.assertIn('Jira', str(caught.exception))
            self.assertNotIn('missing', str(caught.exception))
