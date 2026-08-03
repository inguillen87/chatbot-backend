import socket

import pytest

from tests.conftest import ExternalNetworkBlockedError


def test_external_dns_is_blocked_by_default():
    with pytest.raises(ExternalNetworkBlockedError, match="operation=dns"):
        socket.getaddrinfo("example.invalid", 443)


def test_environment_opt_in_without_marker_is_still_blocked(monkeypatch):
    monkeypatch.setenv("CHATBOC_ALLOW_EXTERNAL_NETWORK_TESTS", "1")

    with pytest.raises(ExternalNetworkBlockedError, match="operation=connect"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(("203.0.113.1", 443))
