import logging

from app import (
    _SENSITIVE_THIRD_PARTY_LOGGERS,
    _suppress_sensitive_third_party_info_logs,
)


def test_sensitive_third_party_request_loggers_are_warning_or_higher():
    loggers = [logging.getLogger(name) for name in _SENSITIVE_THIRD_PARTY_LOGGERS]
    previous_levels = [logger.level for logger in loggers]
    try:
        for logger in loggers:
            logger.setLevel(logging.INFO)

        _suppress_sensitive_third_party_info_logs()

        assert all(logger.getEffectiveLevel() >= logging.WARNING for logger in loggers)
    finally:
        for logger, level in zip(loggers, previous_levels):
            logger.setLevel(level)
