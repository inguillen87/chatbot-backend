import unittest
from unittest.mock import patch, MagicMock
from services.audio_transcription_service import transcribe_audio_from_url

class TestAudioTranscriptionService(unittest.TestCase):

    @patch('services.audio_transcription_service.requests.get')
    @patch('services.audio_transcription_service.speech.SpeechClient')
    def test_transcribe_audio_from_url_success(self, mock_speech_client, mock_requests_get):
        # Mock the requests.get call
        mock_audio_content = b'fake_audio_content'
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = mock_audio_content
        mock_requests_get.return_value = mock_response

        # Mock the speech.SpeechClient and its recognize method
        mock_speech_instance = mock_speech_client.return_value
        mock_recognize_response = MagicMock()
        mock_recognize_response.results = [MagicMock()]
        mock_recognize_response.results[0].alternatives = [MagicMock()]
        mock_recognize_response.results[0].alternatives[0].transcript = 'hello world'
        mock_speech_instance.recognize.return_value = mock_recognize_response

        # Call the function
        result = transcribe_audio_from_url('http://example.com/audio.ogg', 'fake_sid', 'fake_token')

        # Assertions
        self.assertEqual(result, 'hello world')
        mock_requests_get.assert_called_once_with('http://example.com/audio.ogg', auth=('fake_sid', 'fake_token'))
        mock_speech_client.assert_called_once()
        mock_speech_instance.recognize.assert_called_once()

    @patch('services.audio_transcription_service.requests.get')
    def test_transcribe_audio_from_url_download_fails(self, mock_requests_get):
        # Mock the requests.get call to raise an exception
        mock_requests_get.side_effect = Exception('Download failed')

        # Call the function
        result = transcribe_audio_from_url('http://example.com/audio.ogg', 'fake_sid', 'fake_token')

        # Assertions
        self.assertIsNone(result)

    @patch('services.audio_transcription_service.requests.get')
    @patch('services.audio_transcription_service.speech.SpeechClient')
    def test_transcribe_audio_from_url_transcription_fails(self, mock_speech_client, mock_requests_get):
        # Mock the requests.get call
        mock_audio_content = b'fake_audio_content'
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = mock_audio_content
        mock_requests_get.return_value = mock_response

        # Mock the speech.SpeechClient and its recognize method to raise an exception
        mock_speech_instance = mock_speech_client.return_value
        mock_speech_instance.recognize.side_effect = Exception('Transcription failed')

        # Call the function
        result = transcribe_audio_from_url('http://example.com/audio.ogg', 'fake_sid', 'fake_token')

        # Assertions
        self.assertIsNone(result)

if __name__ == '__main__':
    unittest.main()
