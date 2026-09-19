"""Source-linked evaluation menus shared by web and WhatsApp adapters.

Navigation uses explicit option codes, not clinical or legal intent inference.
This module neither sends messages nor accesses people, providers or databases.
"""
from copy import deepcopy
import json
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

PATH = Path(__file__).resolve().parents[1]/'data/conversation_guides/accessible-support.evaluation.v1.json'


def load_guide():
    guide = json.loads(PATH.read_text(encoding='utf-8'))
    if guide.get('contract_version') != 'accessible.support.guide.v1' or guide.get('evaluation_only') is not True:
        raise ValueError('evaluation_guide_invalid')
    nodes = guide['nodes']
    for key, node in nodes.items():
        if key != node['id'] or not node['source_pages'] or any(not 1 <= p <= 14 for p in node['source_pages']):
            raise ValueError('evaluation_source_invalid')
        codes = [a['code'] for a in node['actions']]
        if len(codes) != len(set(codes)) or any(a['target'] not in nodes for a in node['actions']):
            raise ValueError('evaluation_navigation_invalid')
    if any(guide['policy'].values()):
        raise ValueError('evaluation_must_not_activate_operations')
    return guide


def menu_response(node_id='start', selection=None):
    guide = load_guide()
    if node_id not in guide['nodes']:
        raise ValueError('evaluation_node_unknown')
    target = node_id
    if selection is not None:
        code = str(selection).strip().lower()
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


def whatsapp_text(node_id='start', selection=None):
    node = menu_response(node_id, selection)
    lines = ['DEMO · sin trámites ni envíos reales', node['title'], node['text'], '']
    lines.extend(f"{a['code']}. {a['label']}" for a in node['actions'])
    lines.append('Escribí MENU para volver. No ingreses datos personales.')
    return '\n'.join(lines)


def whatsapp_twiml(node_id='start', selection=None):
    response = Element('Response')
    SubElement(response, 'Message').text = whatsapp_text(node_id, selection)
    return tostring(response, encoding='unicode')
