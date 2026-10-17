import os
import subprocess
import sys
import unittest
import tempfile
from pathlib import Path

os.environ.setdefault('OPENAI_API_KEY', 'test-not-a-real-key')
import app as hermes


class HermesTests(unittest.TestCase):
    def setUp(self):
        Path('artifacts').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='artifacts')
        self.addCleanup(self.temp.cleanup)
        hermes.app.config.update(TESTING=True, DATABASE_PATH=self.temp.name + '/test.sqlite')
        self.client = hermes.app.test_client()
        with self.client.session_transaction() as session:
            session['site_authed'] = True

    def test_starts_without_ai_key(self):
        env = dict(os.environ)
        env.pop('OPENAI_API_KEY', None)
        result = subprocess.run([sys.executable, '-c', 'import app'], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ai_missing_configuration_returns_actionable_error(self):
        original = hermes.client
        hermes.client = None
        try:
            response = self.client.post('/generate', json={'transcript': 'Example'})
            self.assertEqual(response.status_code, 503)
            self.assertIn('OPENAI_API_KEY', response.json['error'])
        finally:
            hermes.client = original

    def test_pages_render(self):
        for path in ('/', '/intake', '/scribe', '/display'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_pin_gate(self):
        anonymous = hermes.app.test_client()
        self.assertEqual(anonymous.get('/api/encounters').status_code, 401)
        self.assertEqual(anonymous.post('/unlock', json={'pin': 'wrong'}).status_code, 401)
        self.assertEqual(anonymous.post('/unlock', json={'pin': hermes.SITE_PIN}).status_code, 200)

    def test_encounter_lifecycle(self):
        created = self.client.post('/api/encounters', json={'mrn': 'TEST', 'room': 'Room 2'})
        self.assertEqual(created.status_code, 200)
        eid = created.json['id']
        self.assertEqual(self.client.post(f'/api/encounters/{eid}/select').status_code, 200)
        self.assertEqual(self.client.post(f'/api/encounters/{eid}/status', json={'status': 'ready'}).status_code, 200)
        self.assertEqual(self.client.get(f'/api/encounters/{eid}').json['status'], 'ready')
        self.client.delete(f'/api/encounters/{eid}')
        self.assertEqual(self.client.get(f'/api/encounters/{eid}').status_code, 404)


if __name__ == '__main__':
    unittest.main()
