import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
import certifi
from utils.postgres_tls import normalize_postgres_tls_uri

URI='postgresql://qa:p%40ss@db.example.invalid:5432/qa?sslrootcert=system'

class PostgresTlsTests(unittest.TestCase):
    def test_system_bundle_is_explicit_and_keeps_hostname_verification(self):
        parsed=urlsplit(normalize_postgres_tls_uri(URI))
        query=parse_qs(parsed.query)
        self.assertEqual(query['sslmode'],['verify-full'])
        self.assertEqual(query['sslrootcert'],[str(Path(certifi.where()).resolve())])
        self.assertEqual(parsed.netloc,urlsplit(URI).netloc)
        self.assertEqual(parsed.path,'/qa')

    def test_explicit_full_mode_is_preserved(self):
        result=normalize_postgres_tls_uri(URI+'&sslmode=verify-full')
        self.assertEqual(parse_qs(urlsplit(result).query)['sslmode'],['verify-full'])

    def test_weak_modes_are_not_silently_accepted(self):
        for mode in ['disable','allow','prefer','require','verify-ca','']:
            with self.subTest(mode=mode),self.assertRaisesRegex(ValueError,'postgres_system_ca_requires'):
                normalize_postgres_tls_uri(URI+'&sslmode='+mode)

    def test_duplicate_modes_are_rejected(self):
        with self.assertRaises(ValueError):
            normalize_postgres_tls_uri(URI+'&sslmode=verify-full&sslmode=disable')

    def test_duplicate_roots_are_rejected(self):
        with self.assertRaises(ValueError):
            normalize_postgres_tls_uri(URI+'&sslrootcert=/other.pem')

    def test_custom_ca_is_not_replaced(self):
        custom=URI.replace('system','%2Ftenant%2Fcustom.pem')+'&sslmode=verify-full'
        self.assertEqual(normalize_postgres_tls_uri(custom),custom)

    def test_other_options_and_percent_encoded_credentials_are_preserved(self):
        original=URI+'&connect_timeout=8&application_name=API+candidate&options=-c%20statement_timeout%3D3000'
        output=normalize_postgres_tls_uri(original)
        self.assertEqual(urlsplit(output).netloc,urlsplit(original).netloc)
        before=parse_qs(urlsplit(original).query);after=parse_qs(urlsplit(output).query)
        for key in ['connect_timeout','application_name','options']:self.assertEqual(before[key],after[key])

    def test_sqlite_and_unchanged_postgres_remain_unchanged(self):
        for value in ['sqlite:///:memory:','postgresql://qa@db.example.invalid/qa','postgresql://qa@db.example.invalid/qa?sslmode=require']:
            with self.subTest(value=value):self.assertEqual(normalize_postgres_tls_uri(value),value)

    def test_missing_bundle_fails_without_credentials_in_error(self):
        with patch('utils.postgres_tls.certifi.where',return_value='/missing/ca.pem'):
            with self.assertRaisesRegex(RuntimeError,'^postgres_trusted_ca_bundle_missing$'):
                normalize_postgres_tls_uri(URI)

if __name__=='__main__':unittest.main(verbosity=2)
