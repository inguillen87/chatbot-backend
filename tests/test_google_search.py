import pytest
import unittest
from unittest.mock import patch, MagicMock
from services.google_search import google_search, cache

class GoogleSearchTestCase(unittest.TestCase):
    def setUp(self):
        cache.clear()

    @patch('services.google_search.requests.get')
    def test_caching(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {"items": [{"title": "Test"}]}
        mock_get.return_value = mock_response

        # First call, should call the API
        google_search("test query")
        self.assertEqual(mock_get.call_count, 1)

        # Second call, should be cached
        google_search("test query")
        self.assertEqual(mock_get.call_count, 1)

if __name__ == '__main__':
    unittest.main()
