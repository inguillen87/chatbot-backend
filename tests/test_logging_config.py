import io
import logging
import unittest

from services.logging_config import TruncatingFormatter, log_text_block


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


if __name__ == "__main__":
    unittest.main()

