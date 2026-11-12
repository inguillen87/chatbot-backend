"""Resolve the system-CA intent consistently for bundled libpq clients.

Binary PostgreSQL drivers may link OpenSSL to a different default CA directory
than the Python container. Use our pinned Mozilla bundle for this explicit
intent, preserving certificate and hostname validation; never fall back to
require/disable or alter caller-provided CA paths.
"""
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import certifi


def normalize_postgres_tls_uri(uri: str) -> str:
    parsed = urlsplit(uri)
    if not parsed.scheme.startswith('postgres'):
        return uri
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    roots = [value for key, value in pairs if key == 'sslrootcert']
    if 'system' not in roots:
        return uri
    modes = [value for key, value in pairs if key == 'sslmode']
    if roots != ['system'] or len(modes) > 1 or (modes and modes != ['verify-full']):
        raise ValueError('postgres_system_ca_requires_unambiguous_full_verification')
    trust = str(Path(certifi.where()).resolve())
    if not Path(trust).is_file():
        raise RuntimeError('postgres_trusted_ca_bundle_missing')
    filtered = [(key, value) for key, value in pairs if key not in {'sslrootcert', 'sslmode'}]
    query = urlencode([*filtered, ('sslmode', 'verify-full'), ('sslrootcert', trust)])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))
