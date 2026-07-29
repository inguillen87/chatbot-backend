from __future__ import annotations

import re
from typing import Any


# Stable survey identifiers are intentionally URI-friendly, but never
# normalized: leading/trailing whitespace would change their identity.
SURVEY_LOGICAL_REF_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._~:/%+!$&'()*,;=@-]{0,159}$"
)


def is_canonical_survey_logical_ref(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and SURVEY_LOGICAL_REF_PATTERN.fullmatch(value) is not None
    )


__all__ = ["SURVEY_LOGICAL_REF_PATTERN", "is_canonical_survey_logical_ref"]
