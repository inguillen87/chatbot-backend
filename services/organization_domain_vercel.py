"""Bounded Vercel adapter for the existing frontend project, not auto-provisioning.

The operator supplies a reviewed immutable plan and an owner-scope guard. Add and
verify require an exclusive durable intent before one POST. Unknown outcomes have
only read-only reconciliation. Ownership, TLS and a pending-domain nonce prove
infrastructure; they never certify a real login, CRM operation or human acceptance.
"""
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import time
from urllib.parse import urlencode

from services.organization_domain_binding import (
    DomainBindingError, PlatformObservation, READINESS, VERIFICATION_TTL, normalize_host,
    read_record, revision, _collisions,
)

FRONTEND_PROJECT = 'prj_CEIKYsPgxlSEOBEKjizziSJBsGKf'
CONTRACT = 'organization.domain_vercel.v1'
MAX_BODY = 128 * 1024


class ProvisioningError(RuntimeError):
    """Only fixed, non-secret reasons may leave transport/operator code."""


def require(value, reason):
    if not value:
        raise ProvisioningError(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, 'provider_json_ambiguous')
        value[key] = item
    return value


@dataclass(frozen=True)
class DomainPlan:
    host: str
    tenant_id: int
    tenant_slug: str
    revision: str
    dns_valid_until: int
    frontend_deployment: str
    frontend_source: str
    backend_source: str
    routes_sha256: str
    project_id: str = FRONTEND_PROJECT

    def validate(self, now):
        try:
            normalized = normalize_host(self.host)
        except DomainBindingError:
            raise ProvisioningError('exact_public_hostname_required') from None
        require(normalized == self.host and self.project_id == FRONTEND_PROJECT,
            'existing_frontend_project_required')
        require(type(self.tenant_id) is int and self.tenant_id > 0
            and isinstance(self.tenant_slug, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', self.tenant_slug)
            and isinstance(self.revision, str) and re.fullmatch(r'[0-9a-f]{64}', self.revision),
            'exact_owner_binding_required')
        require(type(now) is int and type(self.dns_valid_until) is int
            and now < self.dns_valid_until <= now + VERIFICATION_TTL,
            'unexpired_owner_dns_required')
        require(isinstance(self.frontend_deployment, str)
            and re.fullmatch(r'dpl_[A-Za-z0-9]{8,128}', self.frontend_deployment)
            and all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}', value)
                for value in (self.frontend_source, self.backend_source))
            and isinstance(self.routes_sha256, str) and re.fullmatch(r'[0-9a-f]{64}', self.routes_sha256),
            'reviewed_source_and_routes_required')


@dataclass(frozen=True)
class Reply:
    status: int
    data: object = field(default=None, repr=False)


class VercelTransport:
    """Fixed verified TLS; no redirect, proxy, retry, URL/body/header logging."""
    def __init__(self, token, *, team_id=None):
        require(isinstance(token, str) and 16 <= len(token) <= 4096
            and token.isascii() and not any(char.isspace() for char in token), 'operator_credential_required')
        require(team_id is None or (isinstance(team_id, str)
            and re.fullmatch(r'team_[A-Za-z0-9]{8,128}', team_id)), 'exact_team_required')
        self._token, self._team = token, team_id

    def __call__(self, method, path, *, body=None):
        require(method in {'GET', 'POST'} and isinstance(path, str)
            and re.fullmatch(r'/v(?:4/aliases/[a-z0-9.-]+|13/deployments/dpl_[A-Za-z0-9]+|'
                r'(?:9|10)/projects/prj_[A-Za-z0-9]+/domains(?:/[a-z0-9.-]+(?:/verify)?)?)', path),
            'fixed_vercel_operation_required')
        require((method == 'GET' and body is None) or (method == 'POST'
            and (path.endswith('/verify') and body is None
                or path.startswith('/v10/') and isinstance(body, dict) and set(body) == {'name'})),
            'fixed_vercel_request_required')
        conn = http.client.HTTPSConnection('api.vercel.com', timeout=5, context=ssl.create_default_context())
        conn.set_debuglevel(0)
        headers = {'Authorization': 'Bearer ' + self._token, 'Accept-Encoding': 'identity'}
        payload = canonical(body) if body is not None else None
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        try:
            query = {'withGitRepoInfo': 'true'} if path.startswith('/v13/deployments/') else {}
            if self._team:
                query['teamId'] = self._team
            target = path + ('?' + urlencode(query) if query else '')
            conn.request(method, target, body=payload, headers=headers)
            response = conn.getresponse()
            if response.status == 404 and method == 'GET':
                return Reply(404)
            require(response.status == 200, 'provider_request_rejected')
            require(response.getheader('Content-Encoding', 'identity') == 'identity', 'provider_body_invalid')
            length = response.getheader('Content-Length')
            require(length is None or (length.isascii() and length.isdigit() and int(length) <= MAX_BODY),
                'provider_body_invalid')
            raw = response.read(MAX_BODY + 1)
            require(len(raw) <= MAX_BODY, 'provider_body_invalid')
            return Reply(response.status, json.loads(raw, object_pairs_hook=unique_object))
        except ProvisioningError:
            raise
        except Exception:
            raise ProvisioningError('provider_outcome_unknown') from None
        finally:
            conn.close()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=5, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, 443), timeout=self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def public_https_readiness(host, params):
    """Resolve only public IPs, pin one checked IP, keep hostname TLS/SNI."""
    try:
        host = normalize_host(host)
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        require(1 <= len(answers) <= 16, 'public_dns_required')
        addresses = [answer[4][0] for answer in answers]
        require(all(ipaddress.ip_address(address).is_global for address in addresses), 'public_dns_required')
        conn = _PinnedHTTPSConnection(host, addresses[0])
        conn.set_debuglevel(0)
        try:
            conn.request('GET', '/api/public/host-readiness?' + urlencode(params),
                headers={'Accept': 'application/json', 'Accept-Encoding': 'identity'})
            response = conn.getresponse()
            require(response.status == 200, 'exact_https_readiness_required')
            require(response.getheader('Content-Encoding', 'identity') == 'identity'
                and response.getheader('Content-Type', '').split(';', 1)[0] == 'application/json',
                'readiness_body_invalid')
            length = response.getheader('Content-Length')
            require(length is None or (length.isascii() and length.isdigit() and int(length) <= MAX_BODY),
                'readiness_body_invalid')
            raw = response.read(MAX_BODY + 1)
            require(len(raw) <= MAX_BODY, 'readiness_body_invalid')
            return Reply(200, json.loads(raw, object_pairs_hook=unique_object))
        finally:
            conn.close()
    except ProvisioningError:
        raise
    except Exception:
        raise ProvisioningError('public_https_unavailable') from None


class IntentJournal:
    """Durable local operator journal; deliberately not a serverless /tmp queue."""
    def __init__(self, directory, plan):
        self.directory = Path(directory)
        require(self.directory.is_dir(), 'existing_private_journal_directory_required')
        self.digest = sha256(canonical(asdict(plan))).hexdigest()

    def path(self, operation, kind):
        require(operation in {'add', 'verify'} and kind in {'intent', 'receipt'}, 'fixed_journal_operation_required')
        return self.directory / (self.digest + '.' + operation + '.' + kind + '.json')

    def _write(self, path, value):
        try:
            with path.open('xb') as stream:
                stream.write(canonical(value))
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            raise ProvisioningError('durable_effect_requires_read_only_reconcile') from None

    def begin(self, operation, now):
        self._write(self.path(operation, 'intent'), {'contract_version': CONTRACT,
            'plan_digest': self.digest, 'operation': operation, 'observed_at': now, 'outcome': 'unknown'})

    def receipt(self, operation, value):
        self._write(self.path(operation, 'receipt'), {'contract_version': CONTRACT,
            'plan_digest': self.digest, 'operation': operation, 'result': value})

    def require_intent(self, operation):
        try:
            raw = self.path(operation, 'intent').read_bytes()
            require(len(raw) <= 2048, 'exact_intent_required')
            row = json.loads(raw, object_pairs_hook=unique_object)
            require(set(row) == {'contract_version', 'plan_digest', 'operation', 'observed_at', 'outcome'}
                and row['contract_version'] == CONTRACT and row['plan_digest'] == self.digest
                and row['operation'] == operation and row['outcome'] == 'unknown', 'exact_intent_required')
        except ProvisioningError:
            raise
        except Exception:
            raise ProvisioningError('exact_intent_required') from None


class VercelDomainAdapter:
    def __init__(self, transport, *, https_probe=public_https_readiness, clock=None):
        self.transport, self.https_probe = transport, https_probe
        self.clock = clock or (lambda: int(time.time()))

    def _domain(self, plan):
        row = self.transport('GET', f'/v9/projects/{plan.project_id}/domains/{plan.host}')
        require(type(row) is Reply, 'provider_response_shape_invalid')
        if row.status == 404:
            return None
        require(row.status == 200 and isinstance(row.data, dict)
            and row.data.get('name') == plan.host and row.data.get('projectId') == plan.project_id
            and type(row.data.get('verified')) is bool
            and all(row.data.get(key) in (None, '') for key in ('redirect', 'gitBranch', 'customEnvironmentId')),
            'exact_project_domain_required')
        return row.data

    def _serving(self, plan):
        deployment = self.transport('GET', '/v13/deployments/' + plan.frontend_deployment)
        require(type(deployment) is Reply and deployment.status == 200 and isinstance(deployment.data, dict),
            'exact_frontend_deployment_required')
        row = deployment.data
        require(row.get('id') == plan.frontend_deployment and row.get('projectId') == plan.project_id
            and row.get('target') == 'production' and row.get('readyState') == 'READY'
            and (row.get('gitSource') or {}).get('sha') == plan.frontend_source,
            'exact_frontend_deployment_required')
        routes = row.get('routes')
        require(isinstance(routes, list) and 1 <= len(routes) <= 256
            and sha256(canonical(routes)).hexdigest() == plan.routes_sha256,
            'reviewed_route_setup_required')
        require(any(isinstance(route, dict) and route.get('src') in ('/api/(.*)', '^/api/(.*)$')
            and route.get('dest') == 'https://api.chatboc.ar/api/$1' for route in routes),
            'same_origin_backend_proxy_required')
        alias = self.transport('GET', '/v4/aliases/' + plan.host)
        require(type(alias) is Reply and alias.status == 200 and isinstance(alias.data, dict)
            and alias.data.get('alias') == plan.host and alias.data.get('projectId') == plan.project_id
            and alias.data.get('deploymentId') == plan.frontend_deployment
            and alias.data.get('redirect') in (None, '')
            and alias.data.get('redirectStatusCode') in (None, '')
            and alias.data.get('deletedAt') is None and alias.data.get('microfrontends') is None,
            'exact_customer_alias_required')

    def attempt(self, operation, plan, journal, *, owner_guard):
        plan.validate(self.clock())
        require(type(journal) is IntentJournal and journal.digest == sha256(canonical(asdict(plan))).hexdigest(),
            'exact_plan_journal_required')
        require(operation in {'add', 'verify'} and owner_guard(plan) is True, 'fresh_owner_approval_required')
        require(not journal.path(operation, 'intent').exists(), 'durable_effect_requires_read_only_reconcile')
        current = self._domain(plan)
        if current is not None and (operation == 'add' or current['verified'] is True):
            return {'state': 'already_assigned', 'verified': current['verified'], 'active': False,
                'auth_e2e_verified': False, 'provider_changes_performed': False}
        require(operation == 'add' or current is not None, 'assigned_domain_required')
        require(owner_guard(plan) is True, 'fresh_owner_approval_required')
        plan.validate(self.clock())
        journal.begin(operation, self.clock())
        try:
            path = (f'/v10/projects/{plan.project_id}/domains' if operation == 'add'
                else f'/v9/projects/{plan.project_id}/domains/{plan.host}/verify')
            reply = self.transport('POST', path, body={'name': plan.host} if operation == 'add' else None)
            require(type(reply) is Reply and reply.status == 200, 'provider_outcome_unknown')
            result = self._reconcile_result(operation, plan)
            journal.receipt(operation, result)
            return result
        except Exception:
            raise ProvisioningError('durable_effect_requires_read_only_reconcile') from None

    def _reconcile_result(self, operation, plan):
        current = self._domain(plan)
        require(current is not None and (operation != 'verify' or current['verified'] is True),
            'provider_effect_not_confirmed')
        return {'state': 'assigned' if operation == 'add' else 'ownership_verified',
            'verified': current['verified'], 'active': False, 'auth_e2e_verified': False,
            'provider_changes_performed': True}

    def reconcile(self, operation, plan, journal):
        plan.validate(self.clock())
        require(type(journal) is IntentJournal and journal.digest == sha256(canonical(asdict(plan))).hexdigest(),
            'exact_plan_journal_required')
        journal.require_intent(operation)
        return self._reconcile_result(operation, plan)

    def observe(self, plan):
        started = self.clock()
        plan.validate(started)
        domain = self._domain(plan)
        require(domain is not None and domain['verified'] is True, 'provider_ownership_pending')
        self._serving(plan)
        nonce = secrets.token_hex(32)
        reply = self.https_probe(plan.host, {'host': plan.host, 'tenant_id': plan.tenant_id,
            'tenant_slug': plan.tenant_slug, 'revision': plan.revision, 'nonce': nonce})
        require(type(reply) is Reply and reply.status == 200 and isinstance(reply.data, dict),
            'exact_pending_runtime_required')
        payload = reply.data
        now = self.clock()
        require(set(payload) == {'contract_version', 'host', 'nonce', 'tenant', 'revision',
            'backend_source', 'observed_at', 'scope', 'active', 'auth_e2e_verified', 'private_assets_included'}
            and payload['contract_version'] == READINESS and payload['host'] == plan.host
            and payload['nonce'] == nonce and payload['tenant'] == {'id': plan.tenant_id, 'slug': plan.tenant_slug}
            and payload['revision'] == plan.revision and payload['backend_source'] == plan.backend_source
            and type(payload['observed_at']) is int and 0 <= now - payload['observed_at'] <= 60
            and payload['scope'] == 'pending_domain_infrastructure_only' and payload['active'] is False
            and payload['auth_e2e_verified'] is False and payload['private_assets_included'] is False,
            'exact_pending_runtime_required')
        domain = self._domain(plan)
        require(domain is not None and domain['verified'] is True, 'provider_ownership_pending')
        self._serving(plan)
        now = self.clock()
        plan.validate(now)
        require(0 <= now - started <= 60, 'fresh_platform_observation_required')
        observation = PlatformObservation(plan.host, plan.project_id, now,
            min(plan.dns_valid_until, now + VERIFICATION_TTL), True, True, True)
        return observation


def activate_from_vercel(adapter, plan, session, tenant_model, user_model, audit_model,
        *, actor_id, authorize, entitlement):
    """Fresh provider/runtime evidence, then atomic existing binding activation."""
    from services.organization_domain_binding import activate_verified_binding
    observation = adapter.observe(plan)
    return activate_verified_binding(session, tenant_model, user_model, audit_model,
        tenant_id=plan.tenant_id, actor_id=actor_id, expected_revision=plan.revision,
        authorize=authorize, entitlement=entitlement, observation=observation,
        expected_project_id=FRONTEND_PROJECT, now=adapter.clock())


def owner_scope_guard(session, tenant_model, user_model, *, actor_id, authorize, entitlement, clock=None):
    """Fresh read-only owner/DNS gate for operator add/verify, no ambient selectors.

    The trusted operator authenticates the actor before constructing this guard.
    It intentionally uses the application's current control-plane authorizer.
    """
    def check(plan):
        from utils.auth_helpers import is_user_auth_disabled
        now = (clock or (lambda: int(time.time())))()
        plan.validate(now)
        tenant = session.query(tenant_model).filter_by(id=plan.tenant_id, slug=plan.tenant_slug).populate_existing().one_or_none()
        actor = session.query(user_model).filter_by(id=actor_id).populate_existing().one_or_none()
        if (tenant is None or actor is None or is_user_auth_disabled(actor)
                or tenant.is_active is not True or authorize(actor, tenant) is not True
                or entitlement(tenant) is not True):
            return False
        row = read_record(tenant)
        try:
            _collisions(session, tenant_model, tenant, plan.host)
        except DomainBindingError:
            return False
        return (row['host'] == plan.host and row['status'] == 'pending_platform'
            and revision(tenant, row) == plan.revision
            and row['dns_verified_at'] <= now < plan.dns_valid_until <= row['dns_valid_until'])
    return check
