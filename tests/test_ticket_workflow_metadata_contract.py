import unittest

from flask import Flask

from routes.ticket import TICKET_WORKFLOW_CONTRACT_VERSION, ticket_bp


class TicketWorkflowMetadataContractTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(ticket_bp)
        self.client = self.app.test_client()

    def test_workflow_metadata_contract(self):
        response = self.client.get("/tickets/workflow/metadata")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["contract_version"], TICKET_WORKFLOW_CONTRACT_VERSION)
        self.assertIn("states", body)
        self.assertIn("transitions", body)
        self.assertIn("cerrado", body["states"])
        self.assertEqual(body["transitions"]["cerrado"], [])


if __name__ == "__main__":
    unittest.main()
