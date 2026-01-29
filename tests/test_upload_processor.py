import unittest
from unittest.mock import patch, MagicMock
from flask import Flask
from services.upload_processor import upload_bp, subir_catalogo

class TestUploadProcessor(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(upload_bp)
        self.app.config['SECRET_KEY'] = 'test'
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()

    @patch('services.upload_processor.User')
    @patch('services.upload_processor.procesar_y_embedear_catalogo')
    @patch('services.upload_processor.verificar_y_crear_coleccion_qdrant')
    @patch('services.upload_processor.get_qdrant_client')
    @patch('services.upload_processor.db')
    @patch('services.upload_processor.os')
    @patch('services.upload_processor.shutil')
    def test_subir_catalogo_with_current_user(self, mock_shutil, mock_os, mock_db, mock_get_qdrant, mock_verify, mock_process, mock_user_cls):
        """Test subir_catalogo accepting current_user argument (bypassing header check)."""

        # Setup mocks
        mock_user = MagicMock()
        mock_user.id = 123
        mock_user.nombre_empresa = "TestCorp"
        mock_user.rubro_id = 1

        # Mock other internal calls to succeed
        mock_os.path.splitext.return_value = ("test", ".csv")
        mock_os.getcwd.return_value = "/tmp"
        mock_os.path.join.return_value = "/tmp/test.csv"
        mock_verify.return_value = True
        mock_process.return_value = 10 # 10 items processed

        # Simulate request context
        with self.app.test_request_context(method='POST'):
            # Patch request inside context
            with patch('services.upload_processor.request') as mock_request:
                mock_file = MagicMock()
                mock_file.filename = "test.csv"
                # request.files is a dict-like object
                mock_request.files = {'file': mock_file}
                mock_request.headers = {}

                response, status_code = subir_catalogo(current_user=mock_user)

                self.assertEqual(status_code, 200)
                self.assertIn("Catálogo procesado", response.json['mensaje'])
                self.assertEqual(response.json['items_procesados'], 10)

                # Verify User query was NOT called
                mock_user_cls.query.filter_by.assert_not_called()

    @patch('services.upload_processor.User')
    @patch('services.upload_processor.procesar_y_embedear_catalogo')
    @patch('services.upload_processor.verificar_y_crear_coleccion_qdrant')
    @patch('services.upload_processor.get_qdrant_client')
    @patch('services.upload_processor.db')
    @patch('services.upload_processor.os')
    @patch('services.upload_processor.shutil')
    def test_subir_catalogo_without_current_user_fails_no_token(self, mock_shutil, mock_os, mock_db, mock_get_qdrant, mock_verify, mock_process, mock_user_cls):
        """Test subir_catalogo without current_user fails if token missing."""

        with self.app.test_request_context(method='POST'):
            with patch('services.upload_processor.request') as mock_request:
                mock_request.headers = {}
                mock_request.files = {} # Won't get checked because token fails first

                response, status_code = subir_catalogo(current_user=None)

                self.assertEqual(status_code, 401)
                self.assertIn("Token no proporcionado", response.json['error'])

    @patch('services.upload_processor.User')
    @patch('services.upload_processor.procesar_y_embedear_catalogo')
    @patch('services.upload_processor.verificar_y_crear_coleccion_qdrant')
    @patch('services.upload_processor.get_qdrant_client')
    @patch('services.upload_processor.db')
    @patch('services.upload_processor.os')
    @patch('services.upload_processor.shutil')
    def test_subir_catalogo_without_current_user_success_with_token(self, mock_shutil, mock_os, mock_db, mock_get_qdrant, mock_verify, mock_process, mock_user_cls):
        """Test subir_catalogo without current_user works if token provided."""

        mock_user = MagicMock()
        mock_user.id = 456
        mock_user_cls.query.filter_by.return_value.first.return_value = mock_user

        mock_os.path.splitext.return_value = ("test", ".xlsx")
        mock_verify.return_value = True
        mock_process.return_value = 5

        with self.app.test_request_context(method='POST'):
             with patch('services.upload_processor.request') as mock_request:
                mock_request.headers = {"Authorization": "Bearer fake_token"}
                mock_file = MagicMock()
                mock_file.filename = "test.xlsx"
                mock_request.files = {'file': mock_file}

                response, status_code = subir_catalogo(current_user=None)

                self.assertEqual(status_code, 200)
                self.assertEqual(response.json['items_procesados'], 5)

                # Verify User query WAS called
                mock_user_cls.query.filter_by.assert_called_with(token="fake_token")

if __name__ == '__main__':
    unittest.main()
