"""Tienda Nube Integration Service."""


def _raise_legacy_transport_disabled():
    raise RuntimeError("legacy_integration_transport_disabled")


class TiendaNubeService:
    @staticmethod
    def get_auth_url(_tenant_id, _redirect_uri):
        """Do not create OAuth URLs with request-derived tenant state."""

        _raise_legacy_transport_disabled()

    @staticmethod
    def handle_callback(_tenant_id, _code, _redirect_uri):
        """Do not exchange a code selected by an unsigned/replayable state."""

        _raise_legacy_transport_disabled()
