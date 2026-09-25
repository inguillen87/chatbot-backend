"""Explicit per-organization rollout. A global switch alone grants nothing."""
from collections.abc import Mapping
import re

KEYS = ('CRM_TASKS_ENABLED', 'CRM_TASKS_TENANT_IDS')


def selected_tenants(raw: object) -> tuple[frozenset[int], bool]:
    if not isinstance(raw, str) or len(raw) > 4096:
        return frozenset(), False
    if not raw.strip():
        return frozenset(), True
    result = set()
    for item in raw.split(','):
        part = item.strip()
        if not re.fullmatch(r'[1-9][0-9]{0,9}', part):
            return frozenset(), False
        number = int(part)
        if number > 2147483647:
            return frozenset(), False
        result.add(number)
    return frozenset(result), True


def task_rollout_decision(config: Mapping, tenant_id: object) -> dict:
    flag = config.get('CRM_TASKS_ENABLED', False)
    enabled = flag is True or isinstance(flag, str) and flag.strip().lower() == 'true'
    selected, valid = selected_tenants(config.get('CRM_TASKS_TENANT_IDS', ''))
    identity_valid = type(tenant_id) is int and tenant_id > 0
    included = identity_valid and valid and tenant_id in selected
    reason = ('invalid_tenant' if not identity_valid else
              'invalid_rollout_config' if not valid else
              'module_disabled' if not enabled else
              'tenant_not_selected' if not included else 'schema_check_required')
    return {'enabled': enabled, 'tenant_selected': included, 'config_valid': valid,
            'eligible': enabled and included, 'reason_code': reason}
