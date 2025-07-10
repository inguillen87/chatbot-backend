# services/external_apis/__init__.py
# This file makes the 'external_apis' directory a Python package.

# Optionally, import client instances or key functions for easier access
# from .google_vision_service import GoogleVisionService
# from .qdrant_service import QdrantService
# from .whatsapp_service import WhatsAppService
# from .speech_to_text_service import SpeechToTextService

# Example:
# vision_service_client = GoogleVisionService()
# qdrant_client = QdrantService(host="localhost", port=6333) # Configuration would come from app config
# whatsapp_sender = WhatsAppService(api_key="...", sender_id="...")
# speech_transcriber = SpeechToTextService()

# For now, these will be instantiated within the services that use them or passed around.
