import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault('OPENAI_API_KEY', 'test-not-a-real-key')
import app as hermes


class PlatformTests(unittest.TestCase):
    def setUp(self):
        Path("artifacts").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir="artifacts")
        hermes.app.config.update(TESTING=True, DATABASE_PATH=self.temp.name + '/test.sqlite')
        self.client = hermes.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def login(self, client=None, user='local'):
        client = client or self.client
        with client.session_transaction() as s:
            s['site_authed'] = True
            s['user_id'] = user

    def test_public_home_and_private_dashboard(self):
        home = self.client.get('/')
        self.assertEqual(home.status_code, 200)
        self.assertIn(b'Sign in', home.data)
        self.assertEqual(self.client.get('/dashboard').status_code, 302)
        self.assertEqual(self.client.get('/api/encounters').status_code, 401)
        self.assertEqual(self.client.post('/clear_note').status_code, 401)
        self.assertEqual(self.client.post('/scribe_clear').status_code, 401)

    def test_save_reopen_and_separate_account(self):
        self.login()
        eid = self.client.post('/api/encounters', json={'mrn':'TEST-1'}).json['id']
        response = self.client.put(f'/api/encounters/{eid}/notes', json={
            'kind':'intake', 'fields':{'chief_complaint':'Synthetic note'}, 'transcript':'Test transcript'})
        self.assertEqual(response.status_code, 200)
        other_client = hermes.app.test_client()
        self.login(other_client)
        note = other_client.get(f'/api/encounters/{eid}').json
        self.assertEqual(note['intake_note']['chief_complaint'], 'Synthetic note')
        self.login(other_client, 'other')
        self.assertEqual(other_client.get(f'/api/encounters/{eid}').status_code, 404)
        self.assertEqual(other_client.get('/api/encounters').json, [])

    def test_select_restores_correct_transcript(self):
        self.login()
        a = self.client.post('/api/encounters', json={'mrn':'A'}).json['id']
        self.client.post('/api/live_transcript', json={'text':'First encounter'})
        b = self.client.post('/api/encounters', json={'mrn':'B'}).json['id']
        self.client.post(f'/api/encounters/{b}/select')
        self.assertEqual(self.client.get('/api/notes').json['transcript'], '')
        self.client.post(f'/api/encounters/{a}/select')
        self.assertEqual(self.client.get('/api/notes').json['transcript'], 'First encounter')

    def test_invalid_ai_preserves_note_and_clear_removes_transcript(self):
        self.login()
        eid = self.client.post('/api/encounters', json={}).json['id']
        self.client.put(f'/api/encounters/{eid}/notes', json={'kind':'scribe','fields':{'subjective':'Keep me'},'transcript':'Full text'})
        with patch.object(hermes, 'chat', return_value='{truncated'):
            self.assertEqual(self.client.post('/scribe_generate', json={'transcript':'Test'}).status_code, 502)
        self.assertEqual(self.client.get(f'/api/encounters/{eid}').json['scribe_note']['subjective'], 'Keep me')
        self.client.post('/scribe_clear')
        self.assertEqual(self.client.get(f'/api/encounters/{eid}').json['transcript'], '')

    def test_live_transcript_is_complete_and_bound_to_encounter(self):
        self.login()
        a = self.client.post('/api/encounters', json={}).json['id']
        b = self.client.post('/api/encounters', json={}).json['id']
        text = 'Synthetic ' * 1000
        self.client.post('/api/live_transcript', json={'text':text}, headers={'X-Hermes-Encounter':a})
        self.assertEqual(self.client.get(f'/api/encounters/{a}').json['transcript'], text)
        self.assertEqual(self.client.get(f'/api/encounters/{b}').json['transcript'], '')

    def test_hosted_missing_configuration_fails_closed(self):
        with patch.dict(os.environ, {'VERCEL':'1', 'DATABASE_URL':'', 'HERMES_PASSWORD_HASH':'', 'SECRET_KEY':''}):
            self.assertEqual(self.client.get('/').status_code, 200)
            self.assertEqual(self.client.post('/unlock', json={'pin':'24601'}).status_code, 503)

    def test_cross_origin_mutations_rejected(self):
        self.login()
        response = self.client.post('/api/encounters', json={}, headers={'Origin':'https://untrusted.example'})
        self.assertEqual(response.status_code, 403)

    def test_login_failure_and_logout(self):
        self.assertEqual(self.client.post('/unlock', json={'pin':'wrong'}).status_code, 401)
        self.assertEqual(self.client.post('/unlock', json={'pin':hermes.SITE_PIN}).status_code, 200)
        self.assertEqual(self.client.get('/dashboard').status_code, 200)
        self.client.post('/logout')
        self.assertEqual(self.client.get('/api/encounters').status_code, 401)


if __name__ == '__main__': unittest.main()
