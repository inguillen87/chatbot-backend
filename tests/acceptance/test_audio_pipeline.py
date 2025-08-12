import pytest
from unittest.mock import patch, MagicMock
from app import create_app, db
from models import User, Rubro, ChatSessionContext
from services.municipio_responder import responder_municipio

@pytest.fixture(scope='module')
def test_client():
    app = create_app('config.TestingConfig')
    with app.test_client() as client:
        with app.app_context():
            db.create_all()
            rubro = Rubro(nombre="municipio", clave="municipio")
            db.session.add(rubro)
            owner_user = User(name="Test Municipio", email="test@municipio.com", rubro=rubro, tipo_chat="municipio")
            db.session.add(owner_user)
            db.session.commit()
            yield client
            db.drop_all()

@patch('services.municipio_responder.transcribe_audio_from_url')
def test_stt_low_confidence(mock_transcribe, test_client):
    mock_transcribe.return_value = {"transcript": "hola", "confidence": 0.7}
    owner_user = User.query.first()
    chat_context = ChatSessionContext(chat_session_id="stt_low", user_id=owner_user.id)
    db.session.add(chat_context)
    db.session.commit()

    response = responder_municipio({"media_url": "http://a.b/c.ogg"}, owner_user, owner_user.rubro, chat_db_context=chat_context)

    assert "¿Es correcto?" in response['message_body']
    assert chat_context.context_data['estado_conversacion'] == 'ESPERANDO_CONFIRMACION_STT'

@patch('services.google_text_to_speech.TextToSpeechService.synthesize_speech')
def test_tts_caching(mock_synthesize, test_client):
    mock_synthesize.return_value = "/static/audio/test.mp3"
    owner_user = User.query.first()
    chat_context = ChatSessionContext(chat_session_id="tts_cache", user_id=owner_user.id)
    db.session.add(chat_context)
    db.session.commit()

    # First call, should call synthesize
    responder_municipio("hola", owner_user, owner_user.rubro, chat_db_context=chat_context)
    mock_synthesize.assert_called_once()

    # Second call, should not call synthesize again
    responder_municipio("hola", owner_user, owner_user.rubro, chat_db_context=chat_context)
    mock_synthesize.assert_called_once()

@patch('services.google_text_to_speech.TextToSpeechService.synthesize_speech')
def test_prefers_audio_flag(mock_synthesize, test_client):
    owner_user = User.query.first()
    viewer_user = User(name="Audio Lover", email="audio@lover.com", prefers_audio=True)
    db.session.add(viewer_user)
    db.session.commit()
    chat_context = ChatSessionContext(chat_session_id="prefers_audio", user_id=owner_user.id)
    db.session.add(chat_context)
    db.session.commit()

    responder_municipio("hola", owner_user, owner_user.rubro, viewer_user=viewer_user, chat_db_context=chat_context)
    mock_synthesize.assert_called_once()

@patch('services.municipio_responder.transcribe_audio_from_url')
def test_auto_learn_prefers_audio(mock_transcribe, test_client):
    mock_transcribe.return_value = {"transcript": "hola", "confidence": 0.9}
    owner_user = User.query.first()
    viewer_user = User(name="Audio Learner", email="audio@learner.com", prefers_audio=False)
    db.session.add(viewer_user)
    db.session.commit()
    chat_context = ChatSessionContext(chat_session_id="auto_learn", user_id=owner_user.id)
    db.session.add(chat_context)
    db.session.commit()

    # First audio message
    responder_municipio({"media_url": "http://a.b/1.ogg"}, owner_user, owner_user.rubro, viewer_user=viewer_user, chat_db_context=chat_context)
    assert not viewer_user.prefers_audio

    # Second audio message
    responder_municipio({"media_url": "http://a.b/2.ogg"}, owner_user, owner_user.rubro, viewer_user=viewer_user, chat_db_context=chat_context)
    assert viewer_user.prefers_audio
