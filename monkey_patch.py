import ssl

if not hasattr(ssl, 'wrap_socket'):
    def _wrap_socket_shim(sock, *args, **kwargs):
        return sock
    ssl.wrap_socket = _wrap_socket_shim

# The following is a patch for a separate circular import issue within eventlet.
# It can be applied here as it's part of the same monkey-patching process.
from eventlet.support import greendns
if not hasattr(greendns, 'resolver'):
    class MockResolver:
        def query(self, *args, **kwargs):
            return []
    greendns.resolver = MockResolver