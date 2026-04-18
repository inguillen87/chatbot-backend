import unittest

from routes.rubros import _education_profile_for_rubro, _widget_preview_for_rubro


class RubrosEducationProfileTestCase(unittest.TestCase):
    def test_detects_private_school_profile(self):
        item = {
            "nombre": "Colegio Privado San Martín",
            "clave": "colegio-privado",
            "descripcion": "Institución educativa con nivel inicial y secundario",
        }
        profile = _education_profile_for_rubro(item)
        self.assertTrue(profile["is_education"])
        self.assertEqual(profile["institution_type"], "private")

    def test_widget_preview_uses_education_preset(self):
        item = {
            "nombre": "Escuela Pública N°10",
            "clave": "escuela-publica",
            "descripcion": "Educación pública",
            "education_profile": {"is_education": True, "institution_type": "public"},
        }
        preview = _widget_preview_for_rubro(item)
        self.assertEqual(preview["preset"], "education-campus")


if __name__ == "__main__":
    unittest.main()
