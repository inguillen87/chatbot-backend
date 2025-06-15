from qdrant_client import QdrantClient

# Usá tu endpoint y API KEY "manage"
client = QdrantClient(
    url="https://0a7ae4c9-8bc2-4ea0-a2e9-c2edc21c1e27.europe-west3-0.gcp.cloud.qdrant.io:6333",
    api_key="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhY2Nlc3MiOiJtIn0.hGjF_2kqXK3OvdxOrL0rOFomQsoihlkExCl2MPGfqJE"
)

client.create_payload_index(
    collection_name="catalogos",
    field_name="user_id",
    field_schema="integer"
)
print("Índice de payload para 'user_id' creado OK.")
