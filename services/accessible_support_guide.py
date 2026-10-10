"""Source-linked evaluation navigation recovered from the original guide.

Explicit option codes only: no natural-language classification, database access,
providers or operational actions. The original content remains byte-for-byte.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / 'data/conversation_guides/accessible-support.evaluation.v1.json'
GUIDE_SHA256 = 'f028f657752ecc74c9cb1d7f4ae8408d210f42a9b0d8ccb6a9afc7bf83d4bf41'


def load_guide():
    content = PATH.read_bytes()
    if sha256(content).hexdigest() != GUIDE_SHA256:
        raise ValueError('evaluation_guide_digest_mismatch')
    guide = json.loads(content)
    if guide.get('contract_version') != 'accessible.support.guide.v1' or guide.get('evaluation_only') is not True:
        raise ValueError('evaluation_guide_invalid')
    nodes = guide['nodes']
    for key, node in nodes.items():
        if key != node['id'] or not node['source_pages'] or any(type(p) is not int or not 1 <= p <= 14 for p in node['source_pages']):
            raise ValueError('evaluation_source_invalid')
        codes = [a['code'] for a in node['actions']]
        if len(codes) != len(set(codes)) or any(a['target'] not in nodes for a in node['actions']):
            raise ValueError('evaluation_navigation_invalid')
    if any(value is not False for value in guide['policy'].values()):
        raise ValueError('evaluation_must_not_activate_operations')
    return guide


def menu_response(node_id='start', selection=None, *, guide=None):
    guide = load_guide() if guide is None else guide
    if not isinstance(node_id, str) or node_id not in guide['nodes']:
        raise ValueError('evaluation_node_unknown')
    target = node_id
    if selection is not None:
        if not isinstance(selection, str) or not 1 <= len(selection) <= 16:
            raise ValueError('evaluation_selection_unknown')
        code = selection.strip().lower()
        if code in ('hola', 'inicio'):
            target = 'start'
        elif code in ('menu', 'menú'):
            target = 'main'
        else:
            action = next((a for a in guide['nodes'][node_id]['actions'] if a['code'] == code), None)
            if action is None:
                raise ValueError('evaluation_selection_unknown')
            target = action['target']
    return deepcopy(guide['nodes'][target])
