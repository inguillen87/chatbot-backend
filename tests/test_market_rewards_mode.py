import unittest
from unittest.mock import patch

from flask import Flask

from routes.market import _runtime_rewards_profile


class MarketRewardsModeTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def test_rewards_profile_demo_mode_uses_demo_service(self):
        self.app.config["ENABLE_DEMO_MODE"] = True
        with self.app.app_context(), patch(
            "routes.market.reward_profile_for_tenant",
            return_value={"balance_resumen": {"saldo_disponible": 100.0}},
        ) as mock_demo:
            payload = _runtime_rewards_profile(tenant_id=9, puntos_en_carrito=20)

        mock_demo.assert_called_once_with(9, 20)
        self.assertEqual(payload["mode"], "demo")
        self.assertEqual(payload["balance_resumen"]["saldo_disponible"], 100.0)

    def test_rewards_profile_demo_mode_off_returns_zeroed_wallet(self):
        self.app.config["ENABLE_DEMO_MODE"] = False
        with self.app.app_context(), patch(
            "routes.market.reward_profile_for_tenant",
            side_effect=AssertionError("reward_profile_for_tenant should not be called when demo mode is off"),
        ):
            payload = _runtime_rewards_profile(tenant_id=9, puntos_en_carrito=12.5)

        self.assertEqual(payload["mode"], "disabled")
        self.assertEqual(payload["balance_resumen"]["saldo_disponible"], 0.0)
        self.assertEqual(payload["balance_resumen"]["puntos_en_carrito"], 12.5)


if __name__ == "__main__":
    unittest.main()
