import os
import pandas as pd
from google.cloud import vision
from google.cloud import documentai_v1beta3 as documentai
from models import db, CatalogoItem

class PymeCatalogMappingService:
    def process_catalog(self, file_path, pyme_id):
        _, file_extension = os.path.splitext(file_path)

        if file_extension == '.xlsx':
            return self._process_excel(file_path, pyme_id)
        elif file_extension == '.pdf':
            return self._process_pdf(file_path, pyme_id)
        elif file_extension in ['.jpg', '.jpeg', '.png']:
            return self._process_image(file_path, pyme_id)
        else:
            raise ValueError("Unsupported file type")

    def _process_excel(self, file_path, pyme_id):
        df = pd.read_excel(file_path)
        for _, row in df.iterrows():
            self._create_catalogo_item(row, pyme_id)

    def _process_pdf(self, file_path, pyme_id):
        # This is a simplified implementation. A real implementation would need to
        # handle different PDF layouts and structures.
        project_id = os.environ.get("GOOGLE_PROJECT_ID")
        location = os.environ.get("GOOGLE_DOCAI_LOCATION")
        processor_id = os.environ.get("GOOGLE_DOCAI_PROCESSOR_ID")

        client = documentai.DocumentProcessorServiceClient()
        name = client.processor_path(project_id, location, processor_id)

        with open(file_path, "rb") as image:
            image_content = image.read()

        document = {"content": image_content, "mime_type": "application/pdf"}

        # Configure the process request
        request = {"name": name, "raw_document": document}

        result = client.process_document(request=request)
        document = result.document

        # For this example, we'll just extract all the text and create a single
        # catalog item. A real implementation would need to parse the text to
        # extract the individual items.
        self._create_catalogo_item({'nombre': document.text}, pyme_id)

    def _process_image(self, file_path, pyme_id):
        client = vision.ImageAnnotatorClient()

        with open(file_path, "rb") as image_file:
            content = image_file.read()

        image = vision.Image(content=content)

        response = client.text_detection(image=image)
        texts = response.text_annotations

        # For this example, we'll just extract all the text and create a single
        # catalog item. A real implementation would need to parse the text to
        # extract the individual items.
        if texts:
            self._create_catalogo_item({'nombre': texts[0].description}, pyme_id)

    def _create_catalogo_item(self, data, pyme_id):
        # This is a simplified implementation. A real implementation would need to
        # handle different column names and data types.
        item = CatalogoItem(
            user_id=pyme_id,
            nombre=data.get('nombre') or data.get('product_name'),
            descripcion=data.get('descripcion') or data.get('description'),
            precio=str(data.get('precio') or data.get('price')),
            cantidad=str(data.get('cantidad') or data.get('quantity')),
            sku=data.get('sku'),
            marca=data.get('marca') or data.get('brand'),
            categoria=data.get('categoria') or data.get('category'),
            unidad=data.get('unidad') or data.get('unit'),
        )
        db.session.add(item)
        db.session.commit()

pyme_catalog_mapping_service = PymeCatalogMappingService()
