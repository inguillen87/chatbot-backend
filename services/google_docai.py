import os
import json
import logging
import re
from google.cloud import documentai_v1beta3 as documentai
from google.oauth2 import service_account

# ✅ Cargar credenciales desde archivo secreto en Render
try:
    with open("/etc/secrets/GOOGLE_SERVICE_KEY_JSON", "r") as f:
        credentials_info = json.load(f)
    credentials = service_account.Credentials.from_service_account_info(credentials_info)
except Exception as e:
    raise RuntimeError(f"❌ Error al cargar credenciales del archivo secreto: {e}")

# 🔍 Procesamiento universal de catálogos PDF
def procesar_catalogo_pdf_google(pdf_path):
    try:
        project_id = "ambient-stack-461118-k7"
        location = "us"
        processor_id = "55c57b09a179531a"

        client = documentai.DocumentProcessorServiceClient(credentials=credentials)
        name = f"projects/{project_id}/locations/{location}/processors/{processor_id}"

        with open(pdf_path, "rb") as file:
            pdf_content = file.read()

        raw_document = documentai.RawDocument(content=pdf_content, mime_type="application/pdf")
        request = documentai.ProcessRequest(name=name, raw_document=raw_document)
        result = client.process_document(request=request)

        texto_extraido = result.document.text
        logging.info("📝 Texto extraído con Google Document AI:")
        logging.info(texto_extraido)

        lineas = [line.strip() for line in texto_extraido.split("\n") if line.strip()]
        productos = []

        for linea in lineas:
            nombre = linea
            precio = ""
            cantidad = ""

            # 💰 Buscar precios: $ 123,45 - 123.45 - 123,45
            precio_match = re.search(r"\$?\s?(\d{1,4}(?:[.,]\d{2})?)", linea)
            if precio_match:
                precio = precio_match.group(1).replace(",", ".")

            # 🔢 Buscar cantidades: x10 - (10) - 10 u
            cantidad_match = re.search(r"\b(?:x\s?)?(\d{1,4})\b", linea)
            if cantidad_match:
                cantidad = cantidad_match.group(1)

            productos.append({
                "nombre": nombre,
                "descripcion": linea,
                "precio": precio,
                "cantidad": cantidad
            })

        return productos

    except Exception as e:
        logging.error(f"❌ Error procesando catálogo con Google Doc AI: {e}")
        return []
