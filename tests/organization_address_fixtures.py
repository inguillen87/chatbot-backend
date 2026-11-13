"""Explicit synthetic fixture reset/corruption, never an application entrypoint."""
from copy import deepcopy

from services.organization_address_config_guard import ABSENT, SECTIONS, address_section_write


def set_fixture_config(session, tenant, config):
    """Allow tests to model out-of-band corruption/reset of each private section."""
    for section in SECTIONS:
        desired = config.get(section, ABSENT)
        if desired is ABSENT and section not in (tenant.configuracion or {}):
            continue
        with address_section_write(session, tenant, section, desired):
            tenant.configuracion = deepcopy(config)
            session.flush()
    tenant.configuracion = deepcopy(config)
    session.flush()
