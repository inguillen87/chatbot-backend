import json
import os
from functools import lru_cache

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
TEMPLATES_PATH = os.path.join(BASE_DIR, 'templates', 'messages.json')

@lru_cache(maxsize=None)
def _load_templates():
    try:
        with open(TEMPLATES_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def get_message(key: str, **kwargs) -> str:
    """Return a formatted message for the given template key."""
    template = _load_templates().get(key, '')
    if not template:
        return ''
    try:
        return template.format(**kwargs)
    except Exception:
        return template
