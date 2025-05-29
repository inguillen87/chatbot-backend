import os
from qdrant_client import QdrantClient

QDRANT_URL = "https://0a7ae4c9-8bc2-4ea0-a2e9-c2edc21c1e27.europe-west3-0.gcp.cloud.qdrant.io"      # poné tu URL real
QDRANT_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhY2Nlc3MiOiJtIn0.hGjF_2kqXK3OvdxOrL0rOFomQsoihlkExCl2MPGfqJE"

qdrant = QdrantClient(
    url=QDRANT_URL,
    api_key=QDRANT_API_KEY
)

# Solo hace falta hacerlo una vez por colección
qdrant.create_payload_index(
    collection_name="catalogos",
    field_name="user_id",
    field_schema="integer"
)

print("✅ Índice creado para user_id en catalogos.")
