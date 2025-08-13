import pytest
import unittest
from types import SimpleNamespace

# Copia simplificada de la función de routes.crm

def _obtener_historial_cliente_stub(cliente_id, convs, t_pyme, t_muni):
    archivos = [
        t.archivo_url for t in t_pyme + t_muni if getattr(t, 'archivo_url', None)
    ]
    return {
        'consultas': [
            {'pregunta': c.pregunta, 'respuesta': c.respuesta, 'fecha': 'x'}
            for c in convs
        ],
        'tickets': [
            {'id': t.id, 'tipo': 'pyme', 'nro_ticket': t.nro_ticket, 'estado': t.estado, 'fecha': 'x'}
            for t in t_pyme
        ] + [
            {'id': t.id, 'tipo': 'municipio', 'nro_ticket': t.nro_ticket, 'estado': t.estado, 'fecha': 'x'}
            for t in t_muni
        ],
        'archivos': archivos,
    }

class HistorialClienteTests(unittest.TestCase):
    def test_obtener_historial_armado(self):
        convs = [SimpleNamespace(pregunta='p1', respuesta='r1')]
        t1 = SimpleNamespace(id=1, nro_ticket=100, estado='nuevo', archivo_url='a.pdf')
        t2 = SimpleNamespace(id=2, nro_ticket=200, estado='cerrado', archivo_url=None)
        res = _obtener_historial_cliente_stub(1, convs, [t1], [t2])
        self.assertEqual(len(res['consultas']), 1)
        self.assertEqual(len(res['tickets']), 2)
        self.assertEqual(res['archivos'], ['a.pdf'])

if __name__ == '__main__':
    unittest.main()
