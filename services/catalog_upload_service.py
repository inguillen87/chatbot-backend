import os
import pandas as pd
import pdfplumber
import logging
from models import db, CatalogoItem

logger = logging.getLogger(__name__)

class CatalogUploadService:
    def process_catalog(self, filepath: str, user_id: int):
        """
        Procesa un archivo de catálogo y actualiza la base de datos.
        """
        logger.info(f"Procesando catálogo desde {filepath} para el usuario {user_id}")

        _, extension = os.path.splitext(filepath)

        if extension == '.xlsx':
            self._process_excel(filepath, user_id)
        elif extension == '.pdf':
            self._process_pdf(filepath, user_id)
        else:
            raise ValueError(f"Formato de archivo no soportado: {extension}")

    def _process_excel(self, filepath: str, user_id: int):
        """
        Procesa un archivo de Excel.
        """
        df = pd.read_excel(filepath)
        self._save_catalog_items(df, user_id)

    def _process_pdf(self, filepath: str, user_id: int):
        """
        Procesa un archivo PDF.
        """
        with pdfplumber.open(filepath) as pdf:
            text = ""
            for page in pdf.pages:
                text += page.extract_text()

        # Aquí necesitaríamos una lógica más sofisticada para parsear el texto del PDF.
        # Por ahora, vamos a asumir un formato simple de "producto: precio" por línea.
        lines = text.split('\n')
        data = []
        for line in lines:
            if ':' in line:
                parts = line.split(':')
                data.append({"nombre": parts[0].strip(), "precio": parts[1].strip()})

        if data:
            df = pd.DataFrame(data)
            self._save_catalog_items(df, user_id)

    def _save_catalog_items(self, df: pd.DataFrame, user_id: int):
        """
        Guarda los items del catálogo en la base de datos.
        """
        # Borrar el catálogo anterior para este usuario
        CatalogoItem.query.filter_by(user_id=user_id).delete()

        for _, row in df.iterrows():
            item = CatalogoItem(
                user_id=user_id,
                nombre=row.get('nombre'),
                descripcion=row.get('descripcion'),
                precio=str(row.get('precio')),
                cantidad=str(row.get('cantidad')),
                sku=row.get('sku'),
                marca=row.get('marca'),
                categoria=row.get('categoria'),
                unidad=row.get('unidad'),
                imagen_url=row.get('imagen_url')
            )
            db.session.add(item)

        db.session.commit()
        logger.info(f"Catálogo actualizado para el usuario {user_id}")

catalog_upload_service = CatalogUploadService()
