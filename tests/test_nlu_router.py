import unittest

from services.nlu.router import route


class TestNLURouter(unittest.TestCase):
    def test_route_tributo(self):
        self.assertEqual(route("Quiero pagar un tributo"), "pagar_tasas")

    def test_route_sanidad_animal(self):
        self.assertEqual(route("Información sobre sanidad animal"), "veterinaria_bromatologia")

    def test_route_averia(self):
        self.assertEqual(route("hay una avería"), "iniciar_reclamo")


if __name__ == '__main__':
    unittest.main()
