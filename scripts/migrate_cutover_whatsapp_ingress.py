"""Initialize/verify the independent WhatsApp cutover ingress database."""

from __future__ import annotations

import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cutover_ingress.core import CutoverIngressSettings
from cutover_ingress.migration import migrate_cutover_ingress


def main() -> int:
    settings = CutoverIngressSettings.from_environ()
    engine = settings.build_engine()
    try:
        revision = migrate_cutover_ingress(engine)
    finally:
        engine.dispose()
    print(
        json.dumps(
            {
                "contract_version": "chatboc.cutover_ingress.migration.v1",
                "database_url_exposed": False,
                "revision": revision,
                "status": "ready",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
