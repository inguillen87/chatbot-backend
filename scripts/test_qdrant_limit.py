import sys
import os
from unittest.mock import MagicMock, patch

# Add project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from services.qdrant_search import buscar_catalogo_qdrant

@patch('services.qdrant_search.get_qdrant_client')
@patch('services.qdrant_search.verificar_y_crear_coleccion_qdrant')
@patch('services.qdrant_search.embed_textos')
def test_limit_cast(mock_embed, mock_verify, mock_get_client):
    # Setup mocks
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_verify.return_value = True
    mock_embed.return_value = [[0.1, 0.2, 0.3]] # Fake vector

    # Call with string limit
    print("Calling buscar_catalogo_qdrant with limit='10' (string)...")
    buscar_catalogo_qdrant(
        user_id=1,
        pregunta="test",
        limite="10", # Intentional string
        coleccion="test_collection"
    )

    # Verify search was called with int limit
    mock_client.search.assert_called_once()
    call_kwargs = mock_client.search.call_args.kwargs
    limit_arg = call_kwargs.get('limit')

    print(f"Limit passed to Qdrant client: {limit_arg} (Type: {type(limit_arg)})")

    if isinstance(limit_arg, int) and limit_arg == 10:
        print("SUCCESS: Limit correctly cast to integer.")
    else:
        print("FAILURE: Limit was not cast correctly.")
        sys.exit(1)

    # Call with invalid string limit
    print("\nCalling buscar_catalogo_qdrant with limit='invalid'...")
    mock_client.reset_mock()
    buscar_catalogo_qdrant(
        user_id=1,
        pregunta="test",
        limite="invalid",
        coleccion="test_collection"
    )

    call_kwargs = mock_client.search.call_args.kwargs
    limit_arg = call_kwargs.get('limit')
    print(f"Limit passed to Qdrant client (fallback): {limit_arg}")

    # Check fallback (DEFAULT_SEARCH_LIMIT is usually 5)
    if isinstance(limit_arg, int):
         print("SUCCESS: Fallback to default integer works.")
    else:
         print("FAILURE: Fallback failed.")
         sys.exit(1)

if __name__ == "__main__":
    test_limit_cast()
