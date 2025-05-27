# services/google_docai.py

import os
from google.oauth2 import service_account
from google.cloud import documentai_v1 as documentai

# Datos reales de tu cuenta
PROJECT_ID = "ambient-stack-461118-k7"
LOCATION = "us"
PROCESSOR_ID = "55c57b09a179531a"
CREDENTIALS_PATH = "data/google_service_key.json"

def procesar_catalogo_pdf_google(path_pdf):
    credentials = service_account.Credentials.from_service_account_file(CREDENTIALS_PATH)
    client = documentai.DocumentUnderstandingServiceClient(credentials=credentials)

    with open(path_pdf, "rb") as f:
        pdf_bytes = f.read()

    name = f"projects/{PROJECT_ID}/locations/{LOCATION}/processors/{PROCESSOR_ID}"

    document = {"content": pdf_bytes, "mime_type": "application/pdf"}
    request = {"name": name, "raw_document": document}
    result = client.process_document(request=request)

    doc = result.document
    productos = []

    for page in doc.pages:
        for table in page.tables:
            for row in table.body_rows:
                try:
                    celdas = [cell.layout.text.strip() for cell in row.cells]
                    if len(celdas) >= 3:
                        productos.append({
                            "nombre": celdas[0][:50],
                            "descripcion": " ".join(celdas),
                            "precio": celdas[-1].replace("$", "").replace(".", "").replace(",", ".").strip(),
                            "cantidad": "1"  # opcional: podés mejorar esto según columna
                        })
                except Exception as e:
                    print("⚠️ Error al procesar fila:", e)

    return productos
