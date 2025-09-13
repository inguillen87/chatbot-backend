"""Backward-compatible import of the main system prompt.

This module re-exports `JULES_SYSTEM_PROMPT` from
`services.chatbot_prompts` to avoid maintaining multiple copies of the
prompt string across the codebase.
"""

from ..chatbot_prompts import JULES_SYSTEM_PROMPT

__all__ = ["JULES_SYSTEM_PROMPT"]

