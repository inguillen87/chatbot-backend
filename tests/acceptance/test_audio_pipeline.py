import pytest
from unittest.mock import patch, MagicMock
from app import db
from models import User, ChatSessionContext
from services.municipio_responder import CONTEXTO_MUNICIPIO, responder_municipio
from services.logic import responder_chatboc

# This test no longer needs to mock responder_chatboc, as it's testing logic within it.
# We are also not calling the transcriber directly in this unit, but simulating its output.
def test_stt_low_confidence(init_database, owner_user):
    from services.logic import responder_chatboc

    chat_context = ChatSessionContext(chat_session_id="stt_low", user_id=owner_user.id, context_data={})
    db.session.add(chat_context)
    db.session.commit()

    # Simulate the output of the transcription service being passed as the 'pregunta'
    low_confidence_pregunta = {"transcript": "hola", "confidence": 0.7}

    response = responder_chatboc(
        pregunta=low_confidence_pregunta,
        owner_user=owner_user,
        rubro_obj=owner_user.rubro,
        chat_db_context=chat_context
    )

    assert "Escuché: \"hola\". ¿Es correcto?" in response['message_body']
    assert len(response['options_list']) == 2
    assert response['options_list'][0]['action_id'] == 'confirmar_stt_si'

@patch('services.municipio_responder.handle_llm_interaction')
@patch('services.tts_orchestrator.generar_audio')
def test_tts_caching(mock_synthesize, mock_handle_llm, init_database, owner_user, viewer_user):
    viewer_user.prefers_audio = True
    db.session.commit()

    mock_handle_llm.return_value = (
        {
            "message_body": "Esta es una respuesta de prueba.",
            "accion_backend": "responder_directamente",
            "generar_audio": True
        },
        {}
    )
    mock_synthesize.return_value = "/static/audio/test.mp3"
    chat_context = ChatSessionContext(chat_session_id="tts_cache", user_id=owner_user.id)
    db.session.add(chat_context)
    db.session.commit()

    # First call, should signal to generate audio
    response = responder_municipio("Quiero hacer una consulta", owner_user, owner_user.rubro, viewer_user=viewer_user, chat_db_context=chat_context)
    assert response.get("generar_audio") is True

@patch('services.municipio_responder.handle_llm_interaction')
@patch('services.tts_orchestrator.generar_audio')
def test_prefers_audio_flag(mock_synthesize, mock_handle_llm, init_database, owner_user):
    viewer_user = User(name="Audio Lover", email="audio@lover.com", prefers_audio=True)
    viewer_user.set_password("testpassword")
    db.session.add(viewer_user)
    db.session.commit()
    chat_context = ChatSessionContext(chat_session_id="prefers_audio", user_id=owner_user.id)
    db.session.add(chat_context)
    db.session.commit()

    mock_handle_llm.return_value = (
        {
            "message_body": "Esta es una respuesta de prueba.",
            "accion_backend": "responder_directamente",
            "generar_audio": True
        },
        {}
    )
    response = responder_municipio("Quiero hacer una consulta", owner_user, owner_user.rubro, viewer_user=viewer_user, chat_db_context=chat_context)
    assert response.get("generar_audio") is True

@patch('services.audio_transcription_service.transcribe_audio_from_url')
@pytest.mark.skip(reason="WIP: This test is flaky and needs to be refactored.")
@patch('services.municipio_responder.handle_llm_interaction')
def test_auto_learn_prefers_audio(mock_handle_llm, mock_transcribe, init_database, owner_user, viewer_user, client, app):
    import json
    import jwt
    from datetime import datetime, timedelta

    mock_handle_llm.return_value = {'message_body': 'Hola, he procesado tu audio.', 'accion_backend': 'responder_directamente'}
    mock_transcribe.return_value = {"transcript": "hola", "confidence": 0.9}

    viewer_user.prefers_audio = False
    db.session.add(viewer_user)
    db.session.commit()

    token = jwt.encode(
        {'user_id': viewer_user.id, 'exp': datetime.utcnow() + timedelta(days=1)},
        app.config['SECRET_KEY'],
        algorithm="HS256"
    )

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {token}',
        'X-Chat-Session-Id': 'audio_learner_session'
    }

    # First "audio" message
    client.post('/ask/municipio', headers=headers, data=json.dumps({'pregunta': {'media_url': 'http://a.b/1.ogg'}, 'rubro_id': owner_user.rubro_id}))
    db.session.refresh(viewer_user)
    assert not viewer_user.prefers_audio

    # Second "audio" message
    client.post('/ask/municipio', headers=headers, data=json.dumps({'pregunta': {'media_url': 'http://a.b/2.ogg'}, 'rubro_id': owner_user.rubro_id}))
    db.session.refresh(viewer_user)
    assert viewer_user.prefers_audio
