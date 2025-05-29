import os
import logging
import requests
from time import sleep

COHERE_API_KEY = os.getenv("COHERE_API_KEY")
COHERE_EMBED_BATCH = 50  # Cambiá este valor si hace falta menos/más por batch

def embed_textos(textos: list[str]) -> list[list[float]]:
    """
    Devuelve embeddings de Cohere para una lista de textos.
    Hace el request en batches si hay muchos textos.
    """
    url = "https://api.cohere.ai/v1/embed"
    headers = {
        "Authorization": f"Bearer {COHERE_API_KEY}",
        "Content-Type": "application/json"
    }
    model = "embed-multilingual-v3.0"
    input_type = "search_document"

    all_embeddings = []

    if not textos or not isinstance(textos, list):
        logging.error("❌ [COHERE] Lista de textos vacía o inválida.")
        print("❌ [COHERE] Lista de textos vacía o inválida.")
        return []

    logging.info(f"➡️ [COHERE] Solicitando embeddings para {len(textos)} textos. Batch size: {COHERE_EMBED_BATCH}")
    print(f"➡️ [COHERE] Solicitando embeddings para {len(textos)} textos. Batch size: {COHERE_EMBED_BATCH}")

    for i in range(0, len(textos), COHERE_EMBED_BATCH):
        batch = textos[i:i+COHERE_EMBED_BATCH]
        payload = {
            "texts": batch,
            "model": model,
            "input_type": input_type
        }
        logging.info(f"➡️ [COHERE] Batch {i//COHERE_EMBED_BATCH + 1}: {len(batch)} textos. Ejemplo: {batch[:2]}")
        print(f"➡️ [COHERE] Batch {i//COHERE_EMBED_BATCH + 1}: {len(batch)} textos. Ejemplo: {batch[:2]}")
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=25)
            logging.info(f"⬅️ [COHERE] Status: {response.status_code}, Body: {response.text[:300]}")
            print(f"⬅️ [COHERE] Status: {response.status_code}")
            response.raise_for_status()
            embeddings = response.json().get("embeddings", [])
            logging.info(f"⬅️ [COHERE] Vectores devueltos en batch: {len(embeddings)}")
            print(f"⬅️ [COHERE] Vectores devueltos en batch: {len(embeddings)}")
            if not embeddings:
                logging.error("❌ [COHERE] Batch sin embeddings.")
                print("❌ [COHERE] Batch sin embeddings.")
            all_embeddings.extend(embeddings)
            sleep(0.5)  # Evita rate limit. Ajustá si hace falta.
        except Exception as e:
            logging.error(f"❌ [COHERE] Error batch {i//COHERE_EMBED_BATCH + 1}: {e}")
            print(f"❌ [COHERE] Error batch {i//COHERE_EMBED_BATCH + 1}: {e}")

    logging.info(f"✅ [COHERE] Embeddings totales generados: {len(all_embeddings)} / {len(textos)}")
    print(f"✅ [COHERE] Embeddings totales generados: {len(all_embeddings)} / {len(textos)}")
    return all_embeddings
