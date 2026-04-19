import unittest

from flask import Flask

from routes.public_resolver import _try_get_demo_tenant


class PublicResolverDemoModeTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def test_demo_tenant_disabled_when_demo_mode_off(self):
        self.app.config["ENABLE_DEMO_MODE"] = False
        with self.app.app_context():
            tenant = _try_get_demo_tenant("bodega")
        self.assertIsNone(tenant)

    def test_demo_tenant_available_when_demo_mode_on(self):
        self.app.config["ENABLE_DEMO_MODE"] = True
        with self.app.app_context():
            tenant = _try_get_demo_tenant("bodega")
        self.assertIsNotNone(tenant)
        self.assertEqual(tenant.slug, "bodega")


if __name__ == "__main__":
    unittest.main()
