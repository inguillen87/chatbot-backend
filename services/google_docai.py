import os
import json
import logging
from google.cloud import documentai_v1beta3 as documentai
from google.oauth2 import service_account

# 🛡️ Cargar credenciales desde variable de entorno
json_str = os.getenv("GOOGLE_SERVICE_KEY_JSON")
if not json_str:
    raise RuntimeError("❌ GOOGLE_SERVICE_KEY_JSON no está definido en las env vars.")

try:
    credentials_info = json.loads(json_str)
    credentials = service_account.Credentials.from_service_account_info(credentials_info)
except Exception as e:
    raise RuntimeError(f"❌ Error al cargar credenciales desde GOOGLE_SERVICE_KEY_JSON: {e}")

# 🔍 Función principal de procesamiento con Document AI
def procesar_catalogo_pdf_google(pdf_path):
    try:
        project_id = "ambient-stack-461118-k7"  # <- CORRECTO
        location = "us"
        processor_id = "55c57b09a179531a"

        client = documentai.DocumentProcessorServiceClient(credentials=credentials)
        name = f"projects/{project_id}/locations/{location}/processors/{processor_id}"

        with open(pdf_path, "rb") as file:
            pdf_content = file.read()

        raw_document = documentai.RawDocument(content=pdf_content, mime_type="application/pdf")

        request = documentai.ProcessRequest(
            name=name,
            raw_document=raw_document
        )

        result = client.process_document(request=request)

        document = result.document
        texto_extraido = document.text

        logging.info("📝 Texto extraído con Google Document AI:")
        logging.info(texto_extraido)

        # 💡 Acá podés parsear líneas en productos, precios, etc.
        items = [{"descripcion": linea.strip()} for linea in texto_extraido.split("\n") if linea.strip()]
        return items

    except Exception as e:
        logging.error(f"❌ Error procesando catálogo con Google Doc AI: {e}")
        return []
