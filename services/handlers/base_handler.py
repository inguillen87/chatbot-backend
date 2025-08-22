import logging

logger = logging.getLogger(__name__)

class BaseMunicipioHandler:
    def __init__(self, context):
        self.context = context

    def handle(self, payload: dict) -> dict | None:
        raise NotImplementedError
