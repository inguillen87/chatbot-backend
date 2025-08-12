
import pytest
import os

pytestmark = [
    pytest.mark.legacy,
    pytest.mark.skipif(os.getenv("NEW_PIPELINE") == "1", reason="Legacy test disabled for new pipeline")
]

class MockGemini:
    def __init__(self, text):
        self.text = text

class MockContent:
    def __init__(self, text):
        self.parts = [MockGemini(text)]

class MockGeminiResponse:
    def __init__(self, text):
        self.candidates = [MockContent(text)]

    def to_dict(self):
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": self.text
                            }
                        ]
                    }
                }
            ]
        }
