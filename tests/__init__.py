import sys
from types import ModuleType
from pathlib import Path

root_dir = Path(__file__).resolve().parents[1]
venv_site = root_dir / 'venv' / 'Lib' / 'site-packages'
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))
if venv_site.exists() and str(venv_site) not in sys.path:
    sys.path.insert(0, str(venv_site))

sys.modules.setdefault('google', ModuleType('google'))
sys.modules.setdefault('google.cloud', ModuleType('google.cloud'))
sys.modules.setdefault('google.cloud.documentai', ModuleType('google.cloud.documentai'))
sys.modules.setdefault('google.cloud.documentai_v1', ModuleType('google.cloud.documentai_v1'))
sys.modules.setdefault('google.api_core', ModuleType('google.api_core'))
sys.modules.setdefault('google.api_core.gapic_v1', ModuleType('google.api_core.gapic_v1'))
sys.modules.setdefault('grpc', ModuleType('grpc'))
oauth2_stub = ModuleType('google.oauth2')
oauth2_stub.service_account = ModuleType('service_account')
oauth2_stub.service_account.Credentials = object
sys.modules.setdefault('google.oauth2', oauth2_stub)
sys.modules.setdefault('google.oauth2.service_account', ModuleType('google.oauth2.service_account'))
sys.modules.setdefault('cohere', ModuleType('cohere'))
sys.modules.setdefault('pydantic', ModuleType('pydantic'))
sys.modules.setdefault('pydantic_core', ModuleType('pydantic_core'))

sys.modules.setdefault('pandas', ModuleType('pandas'))
sys.modules['pandas'].DataFrame = object
sys.modules['pandas'].Series = object
sys.modules.setdefault('spacy', ModuleType('spacy'))
sys.modules.setdefault('numpy', ModuleType('numpy'))
