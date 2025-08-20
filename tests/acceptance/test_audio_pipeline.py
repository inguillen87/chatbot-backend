import pytest
from unittest.mock import patch, MagicMock
from app import db
from models import User, ChatSessionContext
from services.municipio_responder import CONTEXTO_MUNICIPIO, responder_municipio
from services.logic import responder_chatboc
from sqlalchemy.orm.attributes import flag_modified

@patch('services.pymes.llamar_gemini')
@patch('services.audio_transcription_service.transcribe_audio_from_url')
def test_stt_low_confidence(mock_transcribe, mock_llamar_gemini, init_database, owner_user):
    # This now goes to the PYME responder as a fallback
    mock_llamar_gemini.return_value = {'message_body': 'Soy una respuesta mock.', 'accion_backend': 'responder_directamente'}
    mock_transcribe.return_value = {"transcript": "hola", "confidence": 0.7}
    chat_context = ChatSessionContext(chat_session_id="stt_low", user_id=owner_user.id, context_data={})
    db.session.add(chat_context)
    db.session.commit()

    response = responder_chatboc(pregunta={"media_url": "http://a.b/c.ogg"}, owner_user=owner_user, rubro_obj=owner_user.rubro, chat_db_context=chat_context)

    # The fallback pyme responder should have been called.
    assert "Soy una respuesta mock." in response['message_body']


@patch('services.pymes.llamar_gemini')
@patch('services.google_text_to_speech.TextToSpeechService.synthesize_speech')
def test_tts_caching(mock_synthesize, mock_llamar_gemini, init_database, owner_user, viewer_user):
    viewer_user.prefers_audio = True
    db.session.commit()

    mock_llamar_gemini.return_value = {
        "message_body": "Esta es una respuesta de prueba.",
        "accion_backend": "responder_directamente",
    }
    mock_synthesize.return_value = "/static/audio/test.mp3"
    chat_context = ChatSessionContext(chat_session_id="tts_cache", user_id=owner_user.id)
    db.session.add(chat_context)
    db.session.commit()

    # First call, should signal to generate audio
    response = responder_chatboc(
        pregunta="Quiero hacer una consulta",
        owner_user=owner_user,
        rubro_obj=owner_user.rubro,
        viewer_user=viewer_user,
        chat_db_context=chat_context
    )
    # The logic now adds the URL directly and removes the 'generar_audio' flag.
    assert "audio_url" in response
    assert response["audio_url"] == "/static/audio/test.mp3"
    mock_synthesize.assert_called_once_with("Esta es una respuesta de prueba.")

@patch('services.pymes.llamar_gemini')
@patch('services.google_text_to_speech.TextToSpeechService.synthesize_speech')
def test_prefers_audio_flag(mock_synthesize, mock_llamar_gemini, init_database, owner_user):
    viewer_user = User(name="Audio Lover", email="audio@lover.com", prefers_audio=True)
    viewer_user.set_password("testpassword")
    db.session.add(viewer_user)
    db.session.commit()
    chat_context = ChatSessionContext(chat_session_id="prefers_audio", user_id=owner_user.id)
    db.session.add(chat_context)
    db.session.commit()

    mock_llamar_gemini.return_value = {
        "message_body": "Esta es una respuesta de prueba.",
        "accion_backend": "responder_directamente",
    }
    response = responder_chatboc(
        pregunta="Quiero hacer una consulta",
        owner_user=owner_user,
        rubro_obj=owner_user.rubro,
        viewer_user=viewer_user,
        chat_db_context=chat_context
    )
    # The logic now adds the URL directly and removes the 'generar_audio' flag.
    assert "audio_url" in response
    mock_synthesize.assert_called_once_with("Esta es una respuesta de prueba.")

@patch('services.pymes.llamar_gemini')
@patch('services.audio_transcription_service.transcribe_audio_from_url')
def test_auto_learn_prefers_audio(mock_transcribe, mock_llamar_gemini, init_database, owner_user):
    mock_llamar_gemini.return_value = {'message_body': 'Hola, he procesado tu audio.', 'accion_backend': 'responder_directamente'}
    mock_transcribe.return_value = {"transcript": "hola", "confidence": 0.9}
    viewer_user = User(name="Audio Learner", email="audio@learner.com", prefers_audio=False)
    viewer_user.set_password("testpassword")
    db.session.add(viewer_user)
    db.session.commit()

    chat_context = ChatSessionContext(chat_session_id="audio_learn", user_id=owner_user.id, context_data={})
    db.session.add(chat_context)
    db.session.commit()


    # First audio message
    responder_chatboc(pregunta={"media_url": "http://a.b/1.ogg"}, owner_user=owner_user, rubro_obj=owner_user.rubro, viewer_user=viewer_user, chat_db_context=chat_context)
    db.session.refresh(viewer_user)
    assert not viewer_user.prefers_audio
    assert chat_context.context_data.get('audio_input_count') == 1

    # Second audio message
    # We need to mark the context data as modified so SQLAlchemy picks up the change
    flag_modified(chat_context, "context_data")
    db.session.commit()

    responder_chatboc(pregunta={"media_url": "http://a.b/2.ogg"}, owner_user=owner_user, rubro_obj=owner_user.rubro, viewer_user=viewer_user, chat_db_context=chat_context)
    db.session.commit()  # Make sure the change to viewer_user.prefers_audio is saved
    db.session.refresh(viewer_user)
    assert viewer_user.prefers_audio
    assert chat_context.context_data.get('audio_input_count') == 2
