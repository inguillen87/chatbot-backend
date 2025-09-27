# services/input_processor.py
import logging
from typing import Dict, Any, Tuple, Callable, Optional
from services.audio_transcription_service import transcribe_audio_from_url

logger = logging.getLogger(__name__)

class InputProcessor:
    """
    Handles normalization of input from various channels (web, WhatsApp),
    processes media URLs, and performs speech-to-text if needed.
    """

    def __init__(self, speech_to_text_service: Optional[Callable] = None):
        self.stt_service = speech_to_text_service or transcribe_audio_from_url

    def process_input(self, payload: Dict[str, Any], channel: str) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        """
        Processes the raw input payload and returns standardized text, media info, and location info.

        Args:
            payload: The raw input payload from the channel.
                     Expected keys might include 'pregunta' (text),
                     'media_url' (for WhatsApp media), 'NumMedia' (WhatsApp),
                     'uploaded_file_info' (for web uploads with pre-upload),
                     'ubicacion_usuario' (for location).
            channel: The source channel ('web', 'whatsapp', etc.).

        Returns:
            A tuple containing:
            - text_input (str): The normalized text input from the user.
            - media_info (Dict[str, Any]): Information about any attached media
                                           (e.g., {'url': '...', 'mime_type': '...', 'id': '...'}).
            - location_info (Dict[str, Any]): Information about user's location if shared
                                              (e.g., {'lat': ..., 'lon': ...}).
        """
        text_input = ""
        media_info = {}
        location_info = {}

        logger.debug(f"[InputProcessor] Processing input from channel '{channel}'. Payload: {payload}")

        # 1. Extract Text
        if 'pregunta' in payload and isinstance(payload['pregunta'], str):
            text_input = payload['pregunta'].strip()

        # 2. Extract Media
        # For WhatsApp, media might come via 'MediaUrl0', 'NumMedia', 'MediaContentType0'
        if channel == 'whatsapp':
            if payload.get('NumMedia') and int(payload['NumMedia']) > 0:
                # Assuming only one media item for simplicity for now
                media_url = payload.get('MediaUrl0')
                mime_type = payload.get('MediaContentType0')
                if media_url and mime_type:
                    media_info = {'url': media_url, 'mime_type': mime_type, 'source': 'whatsapp'}
                    logger.info(f"WhatsApp media detected: URL='{media_url}', MIME='{mime_type}'")

                    # Speech-to-text for audio files from WhatsApp
                    if mime_type.startswith('audio/') and self.stt_service:
                        try:
                            # The injected service is now the function itself
                            transcribed_text = self.stt_service(media_url, mime_type)
                            if transcribed_text:
                                text_input = f"{text_input} {transcribed_text}".strip() # Append or replace
                                logger.info(f"STT from WhatsApp audio: '{transcribed_text}'")
                                media_info['transcribed_text'] = transcribed_text
                        except Exception as e_stt:
                            logger.error(f"Error during STT for WhatsApp audio {media_url}: {e_stt}")
                            media_info['stt_error'] = str(e_stt)

        # For web uploads that might have been pre-uploaded and info passed in payload
        elif 'uploaded_file_info' in payload and isinstance(payload['uploaded_file_info'], dict):
            # This info is usually richer, e.g. from a file upload endpoint that already stored it
            # and potentially did some initial processing or stored it in ArchivoAdjunto.
            # It might include an 'id' if already saved.
            media_info = payload['uploaded_file_info'] # e.g., {'id': 123, 'url': '...', 'mime_type': '...', 'name': '...'}
            media_info['source'] = media_info.get('source', 'web') # Ensure source is set
            logger.info(f"Web media (uploaded_file_info) detected: {media_info}")
            # STT for web audio uploads can be handled here if `uploaded_file_info` points to audio
            # and `self.stt_service` is available.

        # 3. Extract Location
        if 'ubicacion_usuario' in payload and isinstance(payload['ubicacion_usuario'], dict):
            # Expected format: {'lat': float, 'lon': float, 'accuracy': float (optional)}
            lat = payload['ubicacion_usuario'].get('lat')
            lon = payload['ubicacion_usuario'].get('lon')
            if isinstance(lat, (float, int)) and isinstance(lon, (float, int)):
                location_info = {'lat': float(lat), 'lon': float(lon)}
                if 'accuracy' in payload['ubicacion_usuario']:
                    location_info['accuracy'] = payload['ubicacion_usuario']['accuracy']
                logger.info(f"Location data extracted: {location_info}")
            else:
                logger.warning(f"Received ubicacion_usuario but lat/lon are invalid: {payload['ubicacion_usuario']}")

        # TODO: Add further normalization if needed (e.g., common misspellings, etc.)
        # For now, text_input is just stripped.

        logger.info(f"[InputProcessor] Processed: Text='{text_input[:100]}...', MediaInfo={media_info}, LocationInfo={location_info}")
        return text_input, media_info, location_info

# Example of how it might be used (conceptual)
if __name__ == '__main__': # pragma: no cover
    # Mock STT service for example
    class MockSTT:
        def transcribe_audio_url(self, url, mime_type):
            return f"Texto transcrito del audio en {url}"

    processor = InputProcessor(speech_to_text_service=MockSTT())

    # WhatsApp text
    text, media, loc = processor.process_input({"pregunta": "Hola, qué tal?"}, "whatsapp")
    print(f"WhatsApp Text: Text='{text}', Media={media}, Location={loc}")

    # WhatsApp image
    text, media, loc = processor.process_input(
        {"NumMedia": "1", "MediaUrl0": "http://images.com/img.jpg", "MediaContentType0": "image/jpeg"},
        "whatsapp"
    )
    print(f"WhatsApp Image: Text='{text}', Media={media}, Location={loc}")

    # WhatsApp audio
    text, media, loc = processor.process_input(
        {"pregunta": "Escucha esto:", "NumMedia": "1", "MediaUrl0": "http://audio.com/audio.ogg", "MediaContentType0": "audio/ogg"},
        "whatsapp"
    )
    print(f"WhatsApp Audio: Text='{text}', Media={media}, Location={loc}")

    # Web text with location
    text, media, loc = processor.process_input(
        {"pregunta": "Info sobre reclamos", "ubicacion_usuario": {"lat": -32.0, "lon": -68.0, "accuracy": 10}},
        "web"
    )
    print(f"Web Text + Loc: Text='{text}', Media={media}, Location={loc}")

    # Web pre-uploaded file
    text, media, loc = processor.process_input(
        {"pregunta": "Ver adjunto", "uploaded_file_info": {"id": 123, "url": "/files/doc.pdf", "mime_type": "application/pdf", "name": "documento.pdf"}},
        "web"
    )
    print(f"Web Uploaded File: Text='{text}', Media={media}, Location={loc}")

    # Web text only
    text, media, loc = processor.process_input({"pregunta": "   Necesito ayuda con un trámite.   "}, "web")
    print(f"Web Text Only: Text='{text}', Media={media}, Location={loc}")

    # Input with no text (e.g., just sending an image on WhatsApp)
    text, media, loc = processor.process_input(
        {"NumMedia": "1", "MediaUrl0": "http://images.com/img_only.png", "MediaContentType0": "image/png"},
        "whatsapp"
    )
    print(f"WhatsApp Image Only: Text='{text}', Media={media}, Location={loc}")

    # Input with no text and no media (e.g. just location share)
    text, media, loc = processor.process_input(
        {"ubicacion_usuario": {"lat": -33.0, "lon": -69.0}},
        "web"
    )
    print(f"Web Location Only: Text='{text}', Media={media}, Location={loc}")

    # Empty payload
    text, media, loc = processor.process_input({}, "web")
    print(f"Empty Web Payload: Text='{text}', Media={media}, Location={loc}")

    # WhatsApp payload with no relevant keys
    text, media, loc = processor.process_input({"SmsMessageSid": "SMxxxx", "AccountSid": "ACxxxx"}, "whatsapp")
    print(f"WhatsApp Empty relevant keys: Text='{text}', Media={media}, Location={loc}")

    # Test STT service not provided
    processor_no_stt = InputProcessor()
    text, media, loc = processor_no_stt.process_input(
        {"pregunta": "Audio sin STT:", "NumMedia": "1", "MediaUrl0": "http://audio.com/audio2.ogg", "MediaContentType0": "audio/ogg"},
        "whatsapp"
    )
    print(f"WhatsApp Audio (No STT Service): Text='{text}', Media={media}, Location={loc}")

    # Test STT service error
    class MockSTTError:
        def transcribe_audio_url(self, url, mime_type):
            raise Exception("STT API failed")

    processor_stt_error = InputProcessor(speech_to_text_service=MockSTTError())
    text, media, loc = processor_stt_error.process_input(
        {"NumMedia": "1", "MediaUrl0": "http://audio.com/audio_error.ogg", "MediaContentType0": "audio/ogg"},
        "whatsapp"
    )
    print(f"WhatsApp Audio (STT Error): Text='{text}', Media={media}, Location={loc}")
    assert 'stt_error' in media
    assert media['stt_error'] == "STT API failed"

    # Test invalid location data
    text, media, loc = processor.process_input(
        {"pregunta": "Lugar?", "ubicacion_usuario": {"lat": "invalid", "lon": "data"}},
        "web"
    )
    print(f"Web Invalid Loc: Text='{text}', Media={media}, Location={loc}")
    assert not loc # Location info should be empty
