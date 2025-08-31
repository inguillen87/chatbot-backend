import unittest

from services.nlu.router import route


class TestNLURouter(unittest.TestCase):
    def test_agenda_cultural(self):
        self.assertEqual(route("Que actividades culturales hay?"), "agenda_cultural")

    def test_ultimas_novedades(self):
        self.assertEqual(route("Que hay de nuevo?"), "ultimas_novedades")

    def test_denuncias(self):
        self.assertEqual(route("Quiero denunciar un problema"), "denuncias")


if __name__ == "__main__":
    unittest.main()
