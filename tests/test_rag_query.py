import unittest
from services.rag_query_service import rag_query_service

class TestRAGQueryService(unittest.TestCase):
    def test_retrieve_and_format(self):
        citations = rag_query_service.retrieve_context("mock query", tenant_id=42)

        self.assertEqual(len(citations), 1)
        self.assertEqual(citations[0].source_id, "doc_mock_1")

        formatted = rag_query_service.format_citations_for_prompt(citations)
        self.assertTrue("<retrieved_knowledge>" in formatted)
        self.assertTrue("[1] Source: Mock Document" in formatted)

if __name__ == "__main__":
    unittest.main()
