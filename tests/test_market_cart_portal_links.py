import unittest
from types import SimpleNamespace

from flask import Flask

from routes.market import _empty_cart_summary


class MarketCartPortalLinksTestCase(unittest.TestCase):
    def test_empty_summary_exposes_portal_links_for_frontend_handoff(self):
        tenant = SimpleNamespace(id=11, slug="junin")
        app = Flask(__name__)
        app.config["ENABLE_DEMO_MODE"] = False
        with app.app_context():
            summary = _empty_cart_summary(tenant)

        continuity = summary["continuity"]
        self.assertEqual(continuity["portal_path"], "/junin/portal")
        self.assertEqual(continuity["portal_links"]["home"], "/junin/portal")
        self.assertEqual(continuity["portal_links"]["orders"], "/junin/portal/pedidos")
        self.assertEqual(continuity["portal_links"]["profile"], "/junin/portal/perfil")
        self.assertIsNone(continuity["conversation_id"])


if __name__ == "__main__":
    unittest.main()
