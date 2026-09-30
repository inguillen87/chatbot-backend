"""Release gates for the additive institutional API on the live production baseline."""
from pathlib import Path
import ast
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIVE = '912446bf96f8330664a9dec009ae57dbf935c73c'

class ScopedRelease(unittest.TestCase):
    def test_runtime_and_schema_are_byte_identical_to_live(self):
        paths = ['models.py', 'migrations', 'requirements.txt', 'requirements_fixed.txt',
                 'socket_service.py', 'config.py', 'config', 'gunicorn.conf.py',
                 'scripts/apply_migrations.py', 'build.sh', 'utils/roles.py']
        changed = subprocess.check_output(['git','diff','--name-only',LIVE,'HEAD','--',*paths],cwd=ROOT,text=True)
        self.assertEqual(changed, '')
    def test_eventlet_bootstrap_is_preserved(self):
        text = (ROOT/'app.py').read_text(encoding='utf-8')
        self.assertIn('eventlet.monkey_patch()',text)
        self.assertIn('app.register_blueprint(institutional_assistant_bp)',text)
    def test_writes_remain_fenced_and_sources_private_by_default(self):
        service=(ROOT/'services/institutional_assistant.py').read_text(encoding='utf-8')
        self.assertIn("visibility = 'private'",service)
        self.assertIn('with_for_update()',service)
        self.assertIn('cutover_writer_fence_enabled',service)
    def test_optional_fence_has_no_runtime_dependencies(self):
        tree=ast.parse((ROOT/'cutover_writer_fence.py').read_text(encoding='utf-8'))
        imports={node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node,ast.ImportFrom)}
        self.assertTrue(imports.issubset({'__future__','collections','typing'}))

if __name__=='__main__': unittest.main(verbosity=2)
