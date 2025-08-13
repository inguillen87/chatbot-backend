import unittest
from types import SimpleNamespace
from datetime import datetime


def _obtener_interacciones(chats, pyme_tickets, muni_tickets):
    historial = []
    for c in chats:
        historial.append({
            "tipo": "chat",
            "pregunta": c.pregunta,
            "respuesta": c.respuesta,
            "fecha": c.timestamp.isoformat(),
        })
    for t in pyme_tickets:
        historial.append({
            "tipo": "ticket_pyme",
            "id": t.id,
            "nro_ticket": t.nro_ticket,
            "asunto": t.asunto,
            "estado": t.estado,
            "fecha": t.fecha.isoformat(),
            "archivo": t.archivo_url,
        })
    for t in muni_tickets:
        historial.append({
            "tipo": "ticket_municipio",
            "id": t.id,
            "nro_ticket": t.nro_ticket,
            "asunto": t.asunto,
            "estado": t.estado,
            "fecha": t.fecha.isoformat(),
            "archivo": t.archivo_url,
        })
    historial.sort(key=lambda x: x["fecha"], reverse=True)
    return historial


class CRMInteraccionesTests(unittest.TestCase):
    def test_obtener_interacciones_orden(self):
        chat = SimpleNamespace(pregunta='P', respuesta='R', timestamp=datetime(2023, 1, 2))
        ticket = SimpleNamespace(id=1, nro_ticket='1', asunto='A', estado='nuevo', fecha=datetime(2023, 1, 1), archivo_url=None)
        res = _obtener_interacciones([chat], [ticket], [])
        self.assertEqual(res[0]['tipo'], 'chat')
        self.assertEqual(res[1]['tipo'], 'ticket_pyme')


if __name__ == '__main__':
    unittest.main()
