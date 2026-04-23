import logging
from typing import Optional, Any
from database import db
from models_audit import PromptVersion

logger = logging.getLogger(__name__)

class PromptRegistry:
    """Service to fetch and manage versioned system prompts."""

    def get_active_prompt(self, prompt_key: str, tenant_id: Optional[Any] = None, channel: Optional[str] = None) -> Optional[PromptVersion]:
        """
        Fetches the active prompt definition for a specific key.
        Checks for tenant-specific overrides first, then channel-specific overrides,
        then falls back to the global default.
        """
        query = PromptVersion.query.filter_by(prompt_key=prompt_key, status="active")

        # 1. Try tenant specific
        if tenant_id:
            try:
                tid = int(tenant_id)
                tenant_prompt = query.filter_by(tenant_override=tid).order_by(PromptVersion.created_at.desc()).first()
                if tenant_prompt:
                    return tenant_prompt
            except (ValueError, TypeError):
                pass

        # 2. Try channel specific
        if channel:
            channel_prompt = query.filter_by(tenant_override=None, channel=channel).order_by(PromptVersion.created_at.desc()).first()
            if channel_prompt:
                return channel_prompt

        # 3. Fallback to global active
        return query.filter_by(tenant_override=None, channel=None).order_by(PromptVersion.created_at.desc()).first()

    def render_instructions(self, prompt: PromptVersion, variables: dict) -> str:
        """
        Renders the instruction string by replacing variables (e.g. {user_name}).
        For now, does a simple format() call.
        """
        try:
            return prompt.instructions.format(**variables)
        except KeyError as e:
            logger.warning(f"Missing variable {e} for prompt {prompt.prompt_key}")
            # Fallback to safe substitution if variables are missing
            import string
            class SafeDict(dict):
                def __missing__(self, key):
                    return "{" + key + "}"
            return string.Formatter().vformat(prompt.instructions, (), SafeDict(variables))

prompt_registry = PromptRegistry()
