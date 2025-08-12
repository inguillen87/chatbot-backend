"""
State machine for conversation flows.
"""

class StateMachine:
    def __init__(self, initial_state='inicio'):
        self.state = initial_state

    def transition(self, action, context):
        # This will be expanded based on the user's spec.
        pass
