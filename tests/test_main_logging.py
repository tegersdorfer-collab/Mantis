import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import main  # noqa: F401  (import applies main's logging configuration)


def test_http_loggers_do_not_emit_request_urls_at_info_level():
    """HTTP client URLs can contain Telegram bot tokens and stay out of logs."""
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING
