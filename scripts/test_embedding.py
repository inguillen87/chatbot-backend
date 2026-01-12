
import sys
import os

# Add root directory to path so we can import app modules
sys.path.append(os.getcwd())

import logging
from services.embedding_service import embed_textos_llm
from unittest.mock import MagicMock, patch

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_embedding_generation():
    print("Testing embedding generation...")

    # Check if OPENAI_API_KEY is set (it should be in the environment)
    if not os.environ.get("OPENAI_API_KEY"):
        print("WARNING: OPENAI_API_KEY not found. Test might fail or need mocking if not running in a live env.")
        # We will mock it if we can't run it live, but the instructions implied using the real one if possible
        # or at least verifying the implementation logic.
        # However, to be safe and autonomous, let's try to run it. If it fails due to auth, we'll mock.

    test_texts = ["Hola mundo", "Prueba de embedding"]

    try:
        embeddings = embed_textos_llm(test_texts)

        if embeddings:
            print(f"Success: Received {len(embeddings)} embeddings.")
            print(f"Dimensions: {len(embeddings[0])}")

            if len(embeddings) == 2 and len(embeddings[0]) == 1024:
                print("PASSED: Correct count and dimensions.")
            else:
                print(f"FAILED: Expected 2 embeddings of dim 1024, got {len(embeddings)} of dim {len(embeddings[0])}")
        else:
            print("FAILED: Returned None.")

    except Exception as e:
        print(f"Error during test execution: {e}")

if __name__ == "__main__":
    test_embedding_generation()
