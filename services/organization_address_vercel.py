"""Read-only, exact-host Vercel observation for organization addresses v2.

The caller builds the immutable plan from an authenticated, freshly read binding
and server-owned alias policy. HTTP callers cannot provide an observation. Every
observation rereads the current Production target and the host through verified
TLS; it performs no provider mutation and does not certify a login or CRM action.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import ipaddress
import json
import re
import secrets
import socket
from urllib.parse import urlencode

from services.organization_domain_binding import BLOCKED_SUFFIXES, VERIFICATION_TTL
from services.organization_domain_vercel import (
    FRONTEND_PROJECT, MAX_BODY, ProvisioningError, Reply, VercelDomainAdapter,
    VercelTransport, _PinnedHTTPSConnection, canonical, require, unique_object,
)

READINESS = 'public.tenant_address_readiness.v2'
PLATFORM_RESERVED_LABELS = frozenset({
    'www', 'api', 'api-preview', 'preview', 'app', 'admin', 'superadmin', 'mail',
    'status', 'auth', 'panel', 'frontend', 'backend', 'login', 'static', 'assets',
    'cdn', 'smtp', 'imap', 'pop', 'autodiscover', 'clerk', 'accounts',
})
_HEX64 = re.compile(r'[0-9a-f]{64}')
_SLUG = re.compile(r'[a-z0-9][a-z0-9-]{0,79}')
_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')
_EXPLICIT_ERRORS = frozenset({
    'address_plan_required', 'address_host_invalid', 'address_platform_host_denied',
    'address_custom_host_denied', 'address_alias_policy_invalid', 'address_kind_invalid',
    'address_clock_invalid', 'address_binding_scope_invalid', 'address_binding_expired',
    'existing_frontend_project_required', 'reviewed_source_and_routes_required',
    'exact_production_target_required', 'provider_response_shape_invalid',
    'exact_project_domain_required', 'provider_ownership_pending',
    'exact_frontend_deployment_required', 'reviewed_route_setup_required',
    'same_origin_backend_proxy_required', 'exact_customer_alias_required',
    'exact_pending_runtime_required', 'fresh_platform_observation_required',
    'public_dns_required', 'readiness_request_invalid', 'exact_https_readiness_required',
    'readiness_body_invalid', 'provider_json_ambiguous', 'public_https_unavailable',
    'provider_request_rejected', 'provider_body_invalid', 'provider_outcome_unknown',
    'fixed_vercel_operation_required', 'fixed_vercel_request_required',
    'operator_credential_required', 'exact_team_required',
})


def _public_host(value):
    require(isinstance(value, str) and 1 <= len(value) <= 253
        and value.isascii() and value == value.lower() and value == value.strip(),
        'address_host_invalid')
    labels = value.split('.')
    require(len(labels) >= 2 and re.fullmatch(r'[a-z][a-z0-9-]{1,62}', labels[-1])
        and all(_LABEL.fullmatch(label) for label in labels), 'address_host_invalid')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ProvisioningError('address_host_invalid')
    require(not any(value == suffix[1:] or value.endswith(suffix)
        for suffix in BLOCKED_SUFFIXES if suffix != '.vercel.app'), 'address_host_invalid')
    return value


def _alias(value):
    host = _public_host(value)
    require(host.endswith('.vercel.app') and len(host.split('.')) == 3,
        'address_alias_policy_invalid')
    return host


def validate_address_host(value, kind, allowed_aliases=()):
    """Validate a normalized host; aliases must come from the server's owner map."""
    host = _public_host(value)
    require(type(allowed_aliases) is tuple and len(allowed_aliases) <= 32,
        'address_alias_policy_invalid')
    for alias in allowed_aliases:
        _alias(alias)
    require(len(set(allowed_aliases)) == len(allowed_aliases), 'address_alias_policy_invalid')
    require(kind in ('platform', 'custom'), 'address_kind_invalid')
    if kind == 'platform':
        labels = host.split('.')
        subdomain = (len(labels) == 3 and labels[1:] == ['chatboc', 'ar']
            and re.fullmatch(r'[a-z0-9][a-z0-9-]{1,38}[a-z0-9]', labels[0])
            and labels[0] not in PLATFORM_RESERVED_LABELS)
        alias = host in allowed_aliases and host.endswith('.vercel.app')
        require(subdomain or alias, 'address_platform_host_denied')
    else:
        require(not allowed_aliases, 'address_alias_policy_invalid')
        require(host not in ('chatboc.ar', 'vercel.app')
            and not host.endswith(('.chatboc.ar', '.vercel.app')), 'address_custom_host_denied')
    return host


@dataclass(frozen=True)
class AddressPlan:
    host: str
    tenant_id: int
    tenant_slug: str
    kind: str
    revision: str
    valid_until: int
    frontend_deployment: str
    frontend_source: str
    backend_source: str
    routes_sha256: str
    allowed_aliases: tuple = ()
    project_id: str = FRONTEND_PROJECT

    def validate(self, now):
        validate_address_host(self.host, self.kind, self.allowed_aliases)
        require(self.project_id == FRONTEND_PROJECT, 'existing_frontend_project_required')
        require(type(self.tenant_id) is int and self.tenant_id > 0
            and isinstance(self.tenant_slug, str) and _SLUG.fullmatch(self.tenant_slug)
            and isinstance(self.revision, str) and _HEX64.fullmatch(self.revision),
            'address_binding_scope_invalid')
        require(type(now) is int and now > 0, 'address_clock_invalid')
        require(type(self.valid_until) is int
            and now < self.valid_until <= now + VERIFICATION_TTL, 'address_binding_expired')
        require(isinstance(self.frontend_deployment, str)
            and re.fullmatch(r'dpl_[A-Za-z0-9]{8,128}', self.frontend_deployment)
            and all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}', value)
                for value in (self.frontend_source, self.backend_source))
            and isinstance(self.routes_sha256, str) and _HEX64.fullmatch(self.routes_sha256),
            'reviewed_source_and_routes_required')


def plan_digest(plan):
    require(type(plan) is AddressPlan, 'address_plan_required')
    return sha256(canonical(asdict(plan))).hexdigest()


@dataclass(frozen=True)
class AddressObservation:
    plan_sha256: str
    host: str
    kind: str
    project_id: str
    observed_at: int
    valid_until: int


def _check_probe_params(host, params):
    require(type(params) is dict
        and set(params) == {'host', 'tenant_id', 'tenant_slug', 'kind', 'revision', 'nonce'}
        and params['host'] == host and type(params['tenant_id']) is int and params['tenant_id'] > 0
        and isinstance(params['tenant_slug'], str) and _SLUG.fullmatch(params['tenant_slug'])
        and params['kind'] in ('platform', 'custom')
        and all(isinstance(params[key], str) and _HEX64.fullmatch(params[key])
            for key in ('revision', 'nonce')), 'readiness_request_invalid')


def public_address_https_readiness(host, params):
    """Bounded public GET: a checked IP is pinned while hostname TLS/SNI stays on."""
    try:
        host = _public_host(host)
        _check_probe_params(host, params)
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        require(1 <= len(answers) <= 16, 'public_dns_required')
        addresses = [answer[4][0] for answer in answers]
        require(all(ipaddress.ip_address(address).is_global
            and not ipaddress.ip_address(address).is_multicast for address in addresses),
            'public_dns_required')
        conn = _PinnedHTTPSConnection(host, addresses[0])
        conn.set_debuglevel(0)
        try:
            conn.request('GET', '/api/public/address-readiness?' + urlencode(params),
                headers={'Accept': 'application/json', 'Accept-Encoding': 'identity'})
            response = conn.getresponse()
            require(response.status == 200, 'exact_https_readiness_required')
            require(response.getheader('Content-Encoding', 'identity') == 'identity'
                and response.getheader('Content-Type', '').split(';', 1)[0] == 'application/json'
                and 'no-store' in [part.strip().lower()
                    for part in response.getheader('Cache-Control', '').split(',')],
                'readiness_body_invalid')
            length = response.getheader('Content-Length')
            require(length is None or (length.isascii() and length.isdigit() and int(length) <= MAX_BODY),
                'readiness_body_invalid')
            raw = response.read(MAX_BODY + 1)
            require(len(raw) <= MAX_BODY, 'readiness_body_invalid')
            def invalid_constant(_value):
                raise ProvisioningError('readiness_body_invalid')
            return Reply(200, json.loads(raw, object_pairs_hook=unique_object,
                parse_constant=invalid_constant))
        finally:
            conn.close()
    except ProvisioningError:
        raise
    except Exception:
        raise ProvisioningError('public_https_unavailable') from None


class AddressVercelAdapter(VercelDomainAdapter):
    """Only observation is exposed; inherited mutation methods are disabled."""
    def __init__(self, transport, *, https_probe=public_address_https_readiness, clock=None):
        super().__init__(transport, https_probe=https_probe, clock=clock)

    def attempt(self, *_args, **_kwargs):
        raise ProvisioningError('fixed_vercel_operation_required')

    def reconcile(self, *_args, **_kwargs):
        raise ProvisioningError('fixed_vercel_operation_required')

    def _production(self, plan):
        reply = self.transport('GET', '/v9/projects/' + plan.project_id)
        require(type(reply) is Reply and reply.status == 200 and isinstance(reply.data, dict),
            'exact_production_target_required')
        project = reply.data
        targets = project.get('targets')
        target = targets.get('production') if isinstance(targets, dict) else None
        require(project.get('id') == plan.project_id and isinstance(target, dict)
            and target.get('id') == plan.frontend_deployment
            and target.get('readyState') == 'READY'
            and target.get('target', 'production') == 'production', 'exact_production_target_required')
        for container, key in (('meta', 'githubCommitSha'), ('gitSource', 'sha')):
            value = target.get(container)
            require(value is None or (isinstance(value, dict)
                and value.get(key, plan.frontend_source) == plan.frontend_source),
                'exact_production_target_required')

    def _snapshot(self, plan):
        self._production(plan)
        domain = self._domain(plan)
        require(domain is not None and domain['verified'] is True, 'provider_ownership_pending')
        self._serving(plan)

    def observe(self, plan):
        try:
            require(type(plan) is AddressPlan, 'address_plan_required')
            started = self.clock()
            plan.validate(started)
            self._snapshot(plan)
            nonce = secrets.token_hex(32)
            params = {'host': plan.host, 'tenant_id': plan.tenant_id, 'tenant_slug': plan.tenant_slug,
                'kind': plan.kind, 'revision': plan.revision, 'nonce': nonce}
            reply = self.https_probe(plan.host, params)
            require(type(reply) is Reply and reply.status == 200 and isinstance(reply.data, dict),
                'exact_pending_runtime_required')
            payload = reply.data
            now = self.clock()
            require(type(now) is int and 0 <= now - started <= 60,
                'fresh_platform_observation_required')
            require(set(payload) == {'contract_version', 'host', 'kind', 'nonce', 'tenant', 'revision',
                'backend_source', 'observed_at', 'scope', 'active', 'auth_e2e_verified', 'private_assets_included'}
                and payload['contract_version'] == READINESS and payload['host'] == plan.host
                and payload['kind'] == plan.kind and payload['nonce'] == nonce
                and payload['tenant'] == {'id': plan.tenant_id, 'slug': plan.tenant_slug}
                and type(payload['tenant'].get('id')) is int
                and payload['revision'] == plan.revision and payload['backend_source'] == plan.backend_source
                and type(payload['observed_at']) is int and 0 <= now - payload['observed_at'] <= 60
                and payload['scope'] == 'pending_address_infrastructure_only'
                and payload['active'] is False and payload['auth_e2e_verified'] is False
                and payload['private_assets_included'] is False, 'exact_pending_runtime_required')
            self._snapshot(plan)
            finished = self.clock()
            plan.validate(finished)
            require(0 <= finished - now <= 60 and 0 <= finished - started <= 60,
                'fresh_platform_observation_required')
            return AddressObservation(plan_digest(plan), plan.host, plan.kind, plan.project_id,
                finished, min(plan.valid_until, finished + VERIFICATION_TTL))
        except ProvisioningError as exc:
            if len(exc.args) == 1 and isinstance(exc.args[0], str) and exc.args[0] in _EXPLICIT_ERRORS:
                raise
            raise ProvisioningError('address_provider_observation_failed') from None
        except Exception:
            raise ProvisioningError('address_provider_observation_failed') from None
