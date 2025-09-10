from unittest.mock import MagicMock
from services.municipio_responder import _execute_crear_reclamo

def test_next_state_hint_updates_context():
    contexto = {}
    handler = MagicMock()
    handler.execute.return_value = {'next_state_hint': 'ESPERANDO_DATOS_CONTACTO'}
    _execute_crear_reclamo(handler, {}, contexto)
    assert contexto.get('estado_conversacion') == 'ESPERANDO_DATOS_CONTACTO'
