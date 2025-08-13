import pytest
import unittest
import os
import pandas as pd
from unittest.mock import patch, MagicMock
from services.catalog_upload_service import CatalogUploadService
from models import db, CatalogoItem, User
from app import create_app

@pytest.mark.legacy
class TestCatalogUploadService(unittest.TestCase):

    def setUp(self):
        self.app = create_app('config.TestConfig')
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        # Crear un usuario de prueba
        self.user = User(name="test", email="test@test.com", password_hash="test")
        db.session.add(self.user)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.catalog_upload_service.pd.read_excel')
    def test_process_excel(self, mock_read_excel):
        # Mock de la lectura del archivo Excel
        mock_df = pd.DataFrame({
            'nombre': ['producto 1', 'producto 2'],
            'descripcion': ['desc 1', 'desc 2'],
            'precio': [100, 200],
            'cantidad': [10, 20]
        })
        mock_read_excel.return_value = mock_df

        service = CatalogUploadService()
        service._process_excel('fake_path.xlsx', self.user.id)

        # Verificar que los items se hayan guardado en la base de datos
        items = CatalogoItem.query.filter_by(user_id=self.user.id).all()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].nombre, 'producto 1')
        self.assertEqual(items[1].precio, '200')

    @patch('services.catalog_upload_service.pdfplumber.open')
    def test_process_pdf(self, mock_pdf_open):
        # Mock de la lectura del archivo PDF
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "producto 1: 100\nproducto 2: 200"
        mock_pdf = MagicMock()
        mock_pdf.pages = [mock_page]
        mock_pdf_open.return_value.__enter__.return_value = mock_pdf

        service = CatalogUploadService()
        service._process_pdf('fake_path.pdf', self.user.id)

        # Verificar que los items se hayan guardado en la base de datos
        items = CatalogoItem.query.filter_by(user_id=self.user.id).all()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].nombre, 'producto 1')
        self.assertEqual(items[1].precio, '200')

if __name__ == '__main__':
    unittest.main()
