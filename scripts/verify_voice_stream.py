import sys
import os
import json
import logging
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock extensions before importing service
sys.modules['extensions'] = MagicMock()
sys.modules['extensions'].db = MagicMock()
sys.modules['models'] = MagicMock()
sys.modules['flask'] = MagicMock()
sys.modules['websockets.sync.client'] = MagicMock()
sys.modules['utils.whatsapp'] = MagicMock()
sys.modules['twilio.rest'] = MagicMock()

# Set env vars
os.environ['OPENAI_API_KEY'] = 'fake_key'
os.environ['TWILIO_ACCOUNT_SID'] = 'fake_sid'
os.environ['TWILIO_AUTH_TOKEN'] = 'fake_token'

def verify_voice_stream_service():
    print("Verifying VoiceStreamService...")

    try:
        from services.voice_stream_service import VoiceStreamService
        print("✅ Import successful")
    except ImportError as e:
        print(f"❌ Import failed: {e}")
        return

    # Mock WebSocket
    mock_ws = MagicMock()
    mock_openai_ws = MagicMock()

    # Mock websockets.sync.client.connect to return mock_openai_ws
    with patch('services.voice_stream_service.ws_connect', return_value=mock_openai_ws) as mock_connect, \
         patch('services.voice_stream_service.eventlet') as mock_eventlet:

        service = VoiceStreamService(mock_ws)

        # Test 1: Run sequence
        # We need to break the infinite loop in run()
        # Mock ws.receive to return 'start' message then None (to break loop)

        start_msg = json.dumps({
            "event": "start",
            "start": {
                "streamSid": "stream_123",
                "callSid": "call_123",
                "customParameters": {
                    "from_number": "+123456789",
                    "to_number": "+987654321"
                }
            }
        })

        mock_ws.receive.side_effect = [start_msg, None]

        # Mock resolve_context to return True (simulated DB hit)
        with patch.object(service, '_resolve_context', return_value=True):
             service.run()

        # Assertions
        print("Checking connections...")
        if mock_connect.called:
            print("✅ OpenAI WebSocket connected")
        else:
            print("❌ OpenAI WebSocket NOT connected")

        if mock_ws.receive.called:
            print("✅ Twilio WebSocket received messages")
        else:
             print("❌ Twilio WebSocket did not receive messages")

        # Check if session update was sent to OpenAI
        # We expect at least 2 sends: session.update and response.create
        if mock_openai_ws.send.call_count >= 2:
             print("✅ OpenAI Session Initialized (session.update sent)")
        else:
             print(f"❌ OpenAI Session NOT Initialized. Call count: {mock_openai_ws.send.call_count}")

    print("Verification complete.")

if __name__ == "__main__":
    verify_voice_stream_service()
