import unittest
from types import SimpleNamespace

from routes.public_resolver import _tenant_rubro_profile


class PublicResolverRubroProfileTestCase(unittest.TestCase):
    def test_school_tenant_includes_education_profile(self):
        tenant = SimpleNamespace(
            tipo="pyme",
            pyme=SimpleNamespace(rubro=SimpleNamespace(nombre="Colegio Privado Bilingüe")),
            municipio=None,
        )
        profile = _tenant_rubro_profile(tenant)
        education = profile.get("education_profile") or {}
        self.assertTrue(education.get("is_education"))
        self.assertEqual(education.get("institution_type"), "private")
        self.assertIn("agenda_academica", education.get("modules", []))

    def test_non_school_tenant_has_no_education_profile(self):
        tenant = SimpleNamespace(
            tipo="pyme",
            pyme=SimpleNamespace(rubro=SimpleNamespace(nombre="Ferretería Industrial")),
            municipio=None,
        )
        profile = _tenant_rubro_profile(tenant)
        self.assertNotIn("education_profile", profile)


if __name__ == "__main__":
    unittest.main()
