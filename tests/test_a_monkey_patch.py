import unittest
import eventlet

class TestMonkeyPatch(unittest.TestCase):
    def test_monkey_patch(self):
        eventlet.monkey_patch()
        self.assertTrue(True)
