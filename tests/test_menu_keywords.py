import unittest
from services.municipio_responder import find_global_menu_action

class TestMenuKeywords(unittest.TestCase):
    def test_agenda_keyword(self):
        self.assertEqual(find_global_menu_action("agenda"), "agenda_y_noticias")

    def test_bromatologia_keyword(self):
        self.assertEqual(find_global_menu_action("bromatologia"), "veterinaria_bromatologia")

if __name__ == "__main__":
    unittest.main()
