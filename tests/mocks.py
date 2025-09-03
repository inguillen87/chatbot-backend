class MockLLMPart:
    def __init__(self, text):
        self.text = text

class MockContent:
    def __init__(self, text):
        self.parts = [MockLLMPart(text)]

class MockLLMResponse:
    def __init__(self, text):
        self.text = text
        self.candidates = [MockContent(text)]

    def to_dict(self):
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": self.text}]
                    }
                }
            ]
        }
