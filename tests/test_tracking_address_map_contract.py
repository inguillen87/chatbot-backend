"""Tracking map capabilities come from an exact tenant, never shared geography."""
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services import tracking_experience as tracking


class TrackingAddressMapContractTests(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.socket.connect', side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        self.tenant = SimpleNamespace(id=51, slug='map-fixture', nombre='Fixture', tipo='municipio',
                                      vertical='gobierno', subvertical=None, logo_url=None)
        self.jurisdiction = {'contract_version': 'operations.tenant_jurisdiction.v1',
            'state': 'configured', 'enforced': True, 'city': 'Fixture City',
            'state_name': 'Fixture Province', 'country': 'AR',
            'bounds': {'west': -68.6, 'south': -33.2, 'east': -68.3, 'north': -32.9},
            'source': {'kind': 'tenant_geo_config'}}

    def _build(self, jurisdiction):
        ticket = SimpleNamespace(id=77, nro_ticket='fixture-map', consulta_pin='', direccion='Calle fixture 123',
            distrito=None, latitud=None, longitud=None, comentarios=[], categoria='Servicio', asunto='Consulta',
            canal_ingreso='whatsapp', estado='nuevo', fecha=datetime(2026, 10, 24, tzinfo=timezone.utc))
        with (patch('services.territorial_evidence.resolve_tenant_jurisdiction', return_value=jurisdiction) as resolver,
              patch.object(tracking, '_claim_evidence_index', return_value={}),
              patch.object(tracking, '_claim_attachments', return_value=[]),
              patch.object(tracking, '_claim_support_contract', return_value={})):
            result = tracking.build_claim_tracking_experience(ticket, self.tenant)
        return result, resolver

    def test_address_and_backend_copy_survive_without_a_point(self):
        result, _ = self._build({'state': 'unconfigured', 'enforced': False})
        self.assertEqual(result['location']['address'], 'Calle fixture 123')
        self.assertIsNone(result['map']['destination'])
        self.assertEqual(result['map']['ui']['search_address_label'], 'Buscar dirección en el mapa')
        self.assertIn('Calle+fixture+123', result['map']['maps_search_url'])
        self.assertIs(result['map']['geocoding']['enabled'], False)

    def test_exact_tenant_context_enables_only_scoped_temporary_geocoding(self):
        result, resolver = self._build(self.jurisdiction)
        resolver.assert_called_once_with(self.tenant)
        capability = result['map']['geocoding']
        self.assertIs(capability['enabled'], True)
        self.assertEqual(capability['provider'], 'nominatim')
        self.assertEqual(capability['scope'], {'kind': 'claim', 'code': result['resource']['code'],
                                              'tenant_id': 51, 'tenant_slug': 'map-fixture'})
        self.assertEqual(capability['jurisdiction']['city'], 'Fixture City')
        self.assertEqual(capability['jurisdiction']['bounds'], self.jurisdiction['bounds'])
        self.assertIs(capability['persists_coordinates'], False)
        self.assertIsNone(result['location']['lat'])
        self.assertIsNone(result['location']['lng'])

    def test_partial_or_untrusted_geography_never_enables_a_global_query(self):
        for missing in ('city', 'state_name', 'country', 'bounds', 'source'):
            with self.subTest(missing=missing):
                jurisdiction = dict(self.jurisdiction)
                jurisdiction.pop(missing)
                result, _ = self._build(jurisdiction)
                self.assertIs(result['map']['geocoding']['enabled'], False)
                self.assertIsNone(result['map']['destination'])

    def test_existing_coordinates_remain_the_authority_without_geocoding(self):
        location = {'address': 'Calle fixture 123', 'lat': -33.08, 'lng': -68.46}
        result = tracking._tracking_map(location)
        self.assertEqual(result['destination']['lat'], -33.08)
        self.assertEqual(result['destination']['lng'], -68.46)
        self.assertIn('-33.08,-68.46', result['maps_search_url'])
