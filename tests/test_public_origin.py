"""Exact public origins behind authenticated TLS ingress; no credentials or network."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_fixture import AgentFixture, ScriptedModel
from movielens_agent.agent.settings import Settings
from movielens_agent.api.app import create_app


class PublicOriginTests(unittest.TestCase):
    def test_https_origin_validation_and_dotenv(self):
        self.assertEqual(Settings(public_origin='https://LAB.EXAMPLE:8443').public_origin, 'https://lab.example:8443')
        self.assertEqual(Settings(public_origin='https://192.0.2.10:8443').public_origin, 'https://192.0.2.10:8443')
        for value in ['http://lab.example', 'https://', 'https://*.example', 'https://lab.example/',
                      'https://name:secret@lab.example', 'https://lab.example?q=1', 'https://lab.example#x',
                      'https://lab.example:0', 'https://lab.example:65536', 'https://bad name.example']:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Settings(public_origin=value)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'.env'
            path.write_text('PUBLIC_ORIGIN=https://lab.example:8443\n')
            with patch.dict('os.environ', {}, clear=True):
                self.assertEqual(Settings.load(path).public_origin,'https://lab.example:8443')

    def test_public_host_is_opt_in_and_exact_same_origin_write_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            f=AgentFixture(Path(directory))
            model=ScriptedModel(f.settings,[])
            with TestClient(create_app(f.settings, model)) as local:
                self.assertEqual(local.get('/api/v1/status',headers={'host':'lab.example:8443'}).status_code,400)
            settings=f.settings.model_copy(update={'public_origin':'https://lab.example:8443'})
            with TestClient(create_app(settings, model),base_url='https://lab.example:8443') as public:
                self.assertEqual(public.get('/api/v1/status').status_code,200)
                self.assertEqual(public.get('/static/governance.js').status_code,200)
                self.assertEqual(public.post('/api/v1/sessions',json={},headers={'origin':settings.public_origin}).status_code,201)
                for origin in ['https://attacker.example', 'https://lab.example', 'http://lab.example:8443', 'null']:
                    with self.subTest(origin=origin):
                        self.assertEqual(public.post('/api/v1/sessions',json={},headers={'origin':origin}).status_code,403)
                self.assertEqual(public.get('/api/v1/status',headers={'host':'attacker.example','x-forwarded-host':'lab.example:8443'}).status_code,400)
                self.assertEqual(public.get('http://127.0.0.1/api/v1/status').status_code,200)
                self.assertEqual(public.post('http://127.0.0.1/api/v1/sessions',json={},headers={'origin':'http://127.0.0.1'}).status_code,201)


if __name__ == '__main__':
    unittest.main()
