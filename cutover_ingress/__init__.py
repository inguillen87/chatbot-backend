"""Independent, fail-closed WhatsApp ingress used during database cutovers.

This package intentionally does not import the Flask application, provider
clients, LLM services, or the primary application database.  The HTTP boundary
only validates and durably buffers signed Twilio requests.  A separately
invoked replay worker may inject the existing ``ingest_whatsapp_inbound_turn``
function after the primary writer is ready.
"""

from .app import create_cutover_ingress_app
from .core import (
    BufferedIngressClaim,
    BufferedIngressReceipt,
    CutoverIngressConfigurationError,
    CutoverIngressConflict,
    CutoverIngressIntegrityError,
    CutoverIngressSettings,
    CutoverIngressStore,
    replay_buffered_claim,
    replay_next_into_chatboc_intake,
    replay_next_buffered_ingress,
)
from .migration import CUTOVER_INGRESS_SCHEMA_REVISION, migrate_cutover_ingress

__all__ = (
    "BufferedIngressClaim",
    "BufferedIngressReceipt",
    "CUTOVER_INGRESS_SCHEMA_REVISION",
    "CutoverIngressConfigurationError",
    "CutoverIngressConflict",
    "CutoverIngressIntegrityError",
    "CutoverIngressSettings",
    "CutoverIngressStore",
    "create_cutover_ingress_app",
    "migrate_cutover_ingress",
    "replay_buffered_claim",
    "replay_next_into_chatboc_intake",
    "replay_next_buffered_ingress",
)
