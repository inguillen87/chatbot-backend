"""Create the Qdrant catalog payload index as an explicit maintenance task.

Importing this module is intentionally side-effect free. Run it directly only
after configuring ``QDRANT_URL`` and ``QDRANT_API_KEY`` in the environment.
"""

from __future__ import annotations

import os
from typing import Any

from qdrant_client import QdrantClient


def _required_setting(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def create_catalog_user_id_index(client: Any | None = None) -> None:
    """Create the ``catalogos.user_id`` payload index using explicit config."""

    qdrant_client = client
    if qdrant_client is None:
        qdrant_client = QdrantClient(
            url=_required_setting("QDRANT_URL"),
            api_key=_required_setting("QDRANT_API_KEY"),
        )

    qdrant_client.create_payload_index(
        collection_name="catalogos",
        field_name="user_id",
        field_schema="integer",
    )


def main() -> int:
    create_catalog_user_id_index()
    print("Indice de payload para 'user_id' creado OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
