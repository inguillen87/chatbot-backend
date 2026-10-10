"""No provider calls, credentials or customer databases."""
import unittest
from scripts.stage_vercel_preview import PROJECT, TEAM, deployment_arguments, validate_project, validate_inventory

SHA = 'a' * 40


class FencedPreviewTests(unittest.TestCase):
    def test_deployment_scoped_revision_not_manual_fallback(self):
        args = deployment_arguments(SHA)
        self.assertIn('CHATBOC_DEPLOYMENT_REVISION=' + SHA, args)
        self.assertFalse(any(value.startswith('BACKEND_VERSION=') for value in args))
        self.assertIn('chatbocRelease=' + SHA, args)

    def test_preview_only_and_no_domain_or_capacity_override(self):
        args = deployment_arguments(SHA)
        self.assertEqual(args[args.index('--target') + 1], 'preview')
        for forbidden in ['--prod', '--skip-domain', '--force', '--token', 'promote', 'alias']:
            self.assertNotIn(forbidden, args)

    def test_writers_and_runtime_initialization_stay_disabled(self):
        args = deployment_arguments(SHA)
        self.assertIn('CUTOVER_WRITER_FENCE_ENABLED=true', args)
        for key in ['OUTBOUND_NOTIFICATIONS_ENABLED', 'ENABLE_RUNTIME_SCHEMA_SYNC', 'ENABLE_RUNTIME_TENANT_INIT',
                    'FLASK_ENABLE_RUNTIME_SCHEMA_SYNC', 'FLASK_ENABLE_RUNTIME_TENANT_INIT']:
            self.assertIn(key + '=false', args)

    def test_invalid_revision_is_rejected(self):
        for value in ['', 'main', 'abc1234', 'a'*39, 'a'*41, 'A'*40, SHA+'\n', '--prod', None]:
            with self.subTest(value=value), self.assertRaises(ValueError): deployment_arguments(value)

    def test_only_chatboc_backend_in_authorized_team(self):
        validate_project({'projectId': PROJECT, 'orgId': TEAM})
        for item in [None, [], {}, {'projectId': 'other', 'orgId': TEAM}, {'projectId': PROJECT, 'orgId': 'other'}]:
            with self.subTest(item=item), self.assertRaises(ValueError): validate_project(item)

    def test_container_inventory_accepts_reviewed_sources(self):
        self.assertEqual(validate_inventory({'framework': {'slug': 'container'}, 'files': [
            'app.py', {'path': 'routes/config.py'}, {'file': 'Dockerfile.vercel'}, '.env.example']}), 4)

    def test_rejects_private_artifacts_in_both_path_formats(self):
        for name in ['.env', '.env.preview.local', 'nested/.env.production', '.vercel/project.json',
                     '.git/config', '.venv/lib.py', 'test-evidence/report.json', 'instance/live.sqlite',
                     'instance/data.db', 'keys/signing.pem', '/absolute/app.py', 'C:/app.py', '..\\app.py', '.vercel\\.env.production.local']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_inventory({'framework': {'slug':'container'}, 'files':[name]})

    def test_malformed_inventory_never_authorizes_upload(self):
        for item in [None, [], {}, {'files':[]}, {'files':[{}]}, {'files':[None]}, {'files':['app.py']},
                     {'files':['app.py'], 'framework': None}, {'files':['app.py'], 'framework':{'slug':'vite'}}]:
            with self.subTest(item=item), self.assertRaises(ValueError): validate_inventory(item)


if __name__ == '__main__': unittest.main()
