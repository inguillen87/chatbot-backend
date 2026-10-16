"""WSGI entrypoint for a dedicated ``chatboc-cutover-ingress`` service.

Import intentionally fails when secrets, the separate database, or its exact
schema revision are unavailable.
"""

from .app import create_cutover_ingress_app


app = create_cutover_ingress_app()
