"""Reserve text generation through OpenAI-compatible providers.

Used only when the primary provider rejects a request before processing it
(quota, spend limit, credentials). A provider takes part only if its key is
configured in the environment, so nothing is sent anywhere by default.

Environment:
  LLM_RESERVE_PROVIDERS     order, default "groq,gemini,huggingface,custom"
  GROQ_API_KEY              GROQ_RESERVE_MODEL (comma-separated; the next one is tried when a model is not found)
  GEMINI_API_KEY            GEMINI_RESERVE_MODEL (default gemini-3.6-flash)
  HF_TOKEN                  HF_RESERVE_MODEL (default openai/gpt-oss-120b)
  LLM_RESERVE_CUSTOM_API_KEY, LLM_RESERVE_CUSTOM_BASE_URL, LLM_RESERVE_CUSTOM_MODEL
  LLM_RESERVE_TIMEOUT_SECONDS  per provider, default 12
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_PROVIDERS = {
    'groq': ('GROQ_API_KEY', 'https://api.groq.com/openai/v1', 'GROQ_RESERVE_MODEL', 'llama-3.3-70b-versatile,openai/gpt-oss-120b,openai/gpt-oss-20b'),
    'gemini': ('GEMINI_API_KEY', 'https://generativelanguage.googleapis.com/v1beta/openai',
               'GEMINI_RESERVE_MODEL', 'gemini-3.6-flash'),
    'huggingface': ('HF_TOKEN', 'https://router.huggingface.co/v1', 'HF_RESERVE_MODEL', 'openai/gpt-oss-120b'),
}
_JSON_MODE = frozenset({'groq', 'gemini'})
DEFAULT_ORDER = 'groq,gemini,huggingface,custom'
MAX_OUTPUT_CHARS = 60000


def _configured():
    """Providers with a key, in the configured order: (name, key, base_url, model)."""
    found = []
    order = os.getenv('LLM_RESERVE_PROVIDERS') or DEFAULT_ORDER
    for name in dict.fromkeys(part.strip().lower() for part in order.split(',')):
        if name == 'custom':
            key = (os.getenv('LLM_RESERVE_CUSTOM_API_KEY') or '').strip()
            base = (os.getenv('LLM_RESERVE_CUSTOM_BASE_URL') or '').strip().rstrip('/')
            model = (os.getenv('LLM_RESERVE_CUSTOM_MODEL') or '').strip()
            if key and model and base.startswith('https://'):
                found.append((name, key, base, model))
            continue
        spec = _PROVIDERS.get(name)
        if spec is None:
            continue
        key = (os.getenv(spec[0]) or '').strip()
        if key:
            found.append((name, key, spec[1], (os.getenv(spec[2]) or '').strip() or spec[3]))
    return found


def reserve_available() -> bool:
    return bool(_configured())


def generate_reserve_text(system_prompt: str, user_prompt: str, *, temperature: Optional[float] = None,
                          json_output: bool = False, post: Optional[Callable] = None,
                          timeout_seconds: Optional[float] = None) -> str:
    """First non-empty answer from the configured reserve providers, or ''.

    Logs only the provider name and a bounded outcome: never prompts, answers,
    keys or provider error bodies.
    """
    providers = _configured()
    if not providers:
        return ''
    if post is None:
        import requests
        post = requests.post
    try:
        timeout = min(max(float(os.getenv('LLM_RESERVE_TIMEOUT_SECONDS', '12')), 2.0), 30.0)
    except ValueError:
        timeout = 12.0
    if timeout_seconds is not None:
        timeout = min(max(float(timeout_seconds), 2.0), 30.0)
    system = system_prompt or 'Sos un asistente util.'
    if json_output:
        system += ' Devuelve un unico objeto JSON valido.'
    for name, key, base, models in providers:
        candidates = [m.strip() for m in models.split(',') if m.strip()][:4]
        for position, model in enumerate(candidates):
            body = {'model': model, 'messages': [{'role': 'system', 'content': system},
                                                 {'role': 'user', 'content': str(user_prompt or '')}]}
            if temperature is not None:
                body['temperature'] = float(temperature)
            if json_output and name in _JSON_MODE:
                body['response_format'] = {'type': 'json_object'}
            if name == 'gemini':
                # Short selection and extraction tasks: keep the model's thinking brief.
                body['reasoning_effort'] = 'low'
            try:
                response = post(base + '/chat/completions', json=body, timeout=timeout,
                                headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
                status = int(getattr(response, 'status_code', 0) or 0)
                if status != 200:
                    logger.warning('Reserve LLM provider=%s model_position=%s outcome=http_%s', name, position, status)
                    if status in (400, 404, 413):
                        continue  # model retired, renamed or too small for this request: try the next one
                    break
                text = response.json()['choices'][0]['message']['content']
            except Exception as error:
                logger.warning('Reserve LLM provider=%s outcome=%s', name, type(error).__name__)
                break
            if isinstance(text, str) and text.strip():
                logger.info('Reserve LLM provider=%s model_position=%s outcome=answered', name, position)
                return text.strip()[:MAX_OUTPUT_CHARS]
            logger.warning('Reserve LLM provider=%s outcome=empty', name)
            break
    return ''
