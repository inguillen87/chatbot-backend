"""Exact operator endpoints that own their existing Clerk authentication flow."""
from flask import request

OPERATOR_ENDPOINTS = frozenset({
    'organization_addresses.activation_plan', 'organization_addresses.activate',
})


def is_address_operator_request(req=None):
    req = request if req is None else req
    return req.endpoint in OPERATOR_ENDPOINTS
