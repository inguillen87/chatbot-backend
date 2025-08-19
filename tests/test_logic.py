import unittest
import sys
import os

# Add the project root to the Python path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from services.preferences import is_audio_enabled

class TestLogic(unittest.TestCase):
    def test_is_audio_enabled_by_default(self):
        """
        Tests that is_audio_enabled returns True by default for inclusivity.
        """
        chat_context_data = {}
        self.assertTrue(is_audio_enabled(chat_context_data))

if __name__ == '__main__':
    unittest.main()
