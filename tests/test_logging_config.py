import io
import logging
import unittest

from services.logging_config import (
    PrivacyRedactionFilter,
    TruncatingFormatter,
    log_text_block,
    sanitize_log_message,
)


class LoggingConfigTest(unittest.TestCase):
    def test_truncating_formatter_limits_line_length(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        formatter = TruncatingFormatter("%(message)s", max_length=20)
        handler.setFormatter(formatter)

        logger = logging.getLogger("trunc-test")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        logger.info("x" * 30)
        output = stream.getvalue().strip()
        self.assertTrue(output.endswith("... [truncated 10 chars]"))

    def test_log_text_block_splits_long_strings(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)

        logger = logging.getLogger("block-test")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        log_text_block(logger, "data", "a" * 50, chunk_size=20)
        lines = [l for l in stream.getvalue().splitlines() if l]
        # Expect header line plus chunks of size 20 -> 1 + 3 lines
        self.assertEqual(lines[0], "data (length 50):")
        self.assertEqual(len(lines), 4)

    def test_sanitizer_removes_customer_pii_and_provider_secrets(self) -> None:
        original = (
            "email=vecino@example.com telefono=+54 9 261 555 0101 "
            "DNI: 32877851 direccion='San Martin 123' "
            "Authorization: Bearer live-secret PIN=167779 "
            "lat=-33.1234 lng=-68.5678"
        )

        sanitized = sanitize_log_message(original)

        for private_value in (
            "vecino@example.com",
            "+54 9 261 555 0101",
            "32877851",
            "San Martin 123",
            "live-secret",
            "167779",
            "-33.1234",
            "-68.5678",
        ):
            self.assertNotIn(private_value, sanitized)
        self.assertGreaterEqual(sanitized.count("[REDACTED]"), 6)

    def test_filter_redacts_interpolated_arguments_before_plain_formatter(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(PrivacyRedactionFilter())
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("privacy-filter-test")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        logger.info("Mensaje=%s email=%s", "necesito ayuda", "persona@example.com")

        output = stream.getvalue()
        self.assertNotIn("necesito ayuda", output)
        self.assertNotIn("persona@example.com", output)
        self.assertIn("[REDACTED]", output)

    def test_filter_redacts_provider_exception_details(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(PrivacyRedactionFilter())
        handler.setFormatter(logging.Formatter("%(message)s\n%(exc_text)s"))
        logger = logging.getLogger("privacy-exception-test")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.ERROR)
        logger.propagate = False

        try:
            raise RuntimeError(
                "request failed api_key=live-secret email=persona@example.com"
            )
        except RuntimeError:
            logger.exception("Provider request failed")

        output = stream.getvalue()
        self.assertNotIn("live-secret", output)
        self.assertNotIn("persona@example.com", output)
        self.assertIn("RuntimeError", output)
        self.assertIn("[REDACTED]", output)


if __name__ == "__main__":
    unittest.main()
