import pytest
from unittest.mock import patch, MagicMock
from app import db
from models import User, ChatSessionContext
from services.municipio_responder import CONTEXTO_MUNICIPIO
from services.logic import responder_chatboc

@patch('services.municipio_responder.llamar_gemini')
@patch('services.audio_transcription_service.transcribe_audio_from_url')
def test_stt_low_confidence(mock_transcribe, mock_llamar_gemini, init_database, owner_user):
    mock_llamar_gemini.return_value = {'message_body': 'Soy una respuesta mock.'}
    mock_transcribe.return_value = {"transcript": "hola", "confidence": 0.7}
    chat_context = ChatSessionContext(chat_session_id="stt_low", user_id=owner_user.id, context_data={})
    db.session.add(chat_context)
    db.session.commit()

    response = responder_chatboc(pregunta={"media_url": "http://a.b/c.ogg"}, owner_user=owner_user, rubro_obj=owner_user.rubro, chat_db_context=chat_context)

    assert "¿Es correcto?" in response['message_body']
    assert chat_context.context_data[CONTEXTO_MUNICIPIO]['estado_conversacion'] == 'ESPERANDO_CONFIRMACION_STT'

@patch('services.google_text_to_speech.TextToSpeechService.synthesize_speech')
def test_tts_caching(mock_synthesize, init_database, owner_user, viewer_user):
    viewer_user.prefers_audio = True
    db.session.commit()

    mock_synthesize.return_value = "/static/audio/test.mp3"
    chat_context = ChatSessionContext(chat_session_id="tts_cache", user_id=owner_user.id)
    db.session.add(chat_context)
    db.session.commit()

    # First call, should call synthesize
    responder_chatboc(pregunta="hola", owner_user=owner_user, rubro_obj=owner_user.rubro, current_user=viewer_user, chat_db_context=chat_context)
    mock_synthesize.assert_called_once()

    # Second call, should use cache
    responder_chatboc(pregunta="hola", owner_user=owner_user, rubro_obj=owner_user.rubro, current_user=viewer_user, chat_db_context=chat_context)
    mock_synthesize.assert_called_once() # Still called only once

@patch('services.google_text_to_speech.TextToSpeechService.synthesize_speech')
def test_prefers_audio_flag(mock_synthesize, init_database, owner_user):
    viewer_user = User(name="Audio Lover", email="audio@lover.com", prefers_audio=True)
    viewer_user.set_password("testpassword")
    db.session.add(viewer_user)
    db.session.commit()
    chat_context = ChatSessionContext(chat_session_id="prefers_audio", user_id=owner_user.id)
    db.session.add(chat_context)
    db.session.commit()

    responder_chatboc(pregunta="hola", owner_user=owner_user, rubro_obj=owner_user.rubro, current_user=viewer_user, chat_db_context=chat_context)
    mock_synthesize.assert_called_once()

@patch('services.municipio_responder.llamar_gemini')
@patch('services.audio_transcription_service.transcribe_audio_from_url')
def test_auto_learn_prefers_audio(mock_transcribe, mock_llamar_gemini, init_database, owner_user):
    mock_llamar_gemini.return_value = {'message_body': 'Hola, he procesado tu audio.'}
    mock_transcribe.return_value = {"transcript": "hola", "confidence": 0.9}
    viewer_user = User(name="Audio Learner", email="audio@learner.com", prefers_audio=False)
    viewer_user.set_password("testpassword")
    db.session.add(viewer_user)
    db.session.commit()
    class MockChatContext:
        def __init__(self):
            self.context_data = {}

    chat_context = MockChatContext()

    # First audio message
    responder_chatboc(pregunta={"media_url": "http://a.b/1.ogg"}, owner_user=owner_user, rubro_obj=owner_user.rubro, current_user=viewer_user, chat_db_context=chat_context)
    assert not viewer_user.prefers_audio

    # Second audio message
    responder_chatboc(pregunta={"media_url": "http://a.b/2.ogg"}, owner_user=owner_user, rubro_obj=owner_user.rubro, current_user=viewer_user, chat_db_context=chat_context)
    assert viewer_user.prefers_audio
