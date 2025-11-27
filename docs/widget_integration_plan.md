# Widget Integration and Multi-Tenant SaaS Hardening Plan

This document outlines the action plan to stabilize the public widget (e.g., the Junín example), align cart/login redirections with each tenant's marketplace, and reinforce multi-tenant security for the Chatboc platform.

## Goals
- Keep the chat widget stable across page refreshes or server restarts.
- Guarantee that cart actions in the widget redirect to the correct tenant marketplace (e.g., `/m/junin/cart` or `/market`).
- Maintain consistent identity across channels (web widget, WhatsApp, admin) to accumulate user points and survey responses.
- Enforce strict tenant isolation, permissions, and observability.

## Token Stability for `integracion.tsx` (Widget Embed)
1. **Short-lived access + refresh tokens**: Issue a short-lived widget access token (JWT with tenant slug and user/channel metadata) plus an HTTP-only refresh token stored on the widget host. On reload, swap the refresh token for a new access token instead of invalidating the session.
2. **Deterministic tenant scoping**: Include `tenant_id`, `tenant_slug`, and `allowed_origins` in the JWT claims. Reject tokens whose origin or tenant mismatch to avoid cross-tenant leakage when embedding the widget in multiple sites.
3. **Key rotation with JWKS**: Serve a JWKS endpoint so `integracion.tsx` can validate signatures without redeploying when keys rotate. Keep `kid` in the JWT header and fetch JWKS with caching.
4. **Graceful fallback**: If the widget cannot refresh the token, render a limited, read-only mode with a clear "Reconectar" action that triggers re-authentication to reduce perceived downtime.

## Cart and Marketplace Redirection Flow
1. **Canonical marketplace URL per tenant**: Persist per-tenant public base URLs (e.g., `https://chatboc.ar/m/junin`) and expose them in the widget bootstrap payload. When a cart action is triggered, build links from this base plus `/cart`, `/market`, or `/checkout`.
2. **Auth-required actions**: If the widget detects no valid user session, redirect cart clicks to the tenant login/register screen with a `redirect_uri` pointing back to the intended cart route. After authentication, restore cart state using a signed cart token or cart ID tied to the tenant.
3. **Multi-channel cart continuity**: Store cart records with `tenant_id`, `channel` (web, WhatsApp, widget), and `contact_key` (phone/email/device). When the same contact returns via another channel, merge carts server-side under the tenant constraints.

## Identity, Points, and Survey Cohesion
1. **Unified contact graph**: Normalize identity into a `contact` entity keyed by phone/email/document. Link channel-specific identities (WhatsApp number, widget user/pass, admin user) to the same contact to aggregate points and survey responses.
2. **Per-tenant survey campaigns**: Tag surveys with `tenant_id` and `campaign_id`. When a user completes a survey from any channel, create a `survey_response` linked to the contact and tenant and increment points via a single `award_points(contact_id, tenant_id, source)` routine.
3. **DNI/phone validation**: Allow optional verification steps (OTP to phone or email) from the widget to harden identity before crediting high-value points or enabling purchases.

## Permissions and Security
1. **Role matrix**: Define roles per tenant (e.g., `admin`, `gestor`, `analista`, `visor`) with scoped abilities (catalog editing, survey creation, refunds). Apply RBAC checks on every admin route and action.
2. **Tenant isolation**: All API queries must filter by `tenant_id`. Add automated tests that assert cross-tenant access is forbidden for carts, surveys, and contacts.
3. **Audit and rate limits**: Log sensitive actions (token refresh, checkout, survey submission) with tenant and contact IDs. Add per-tenant rate limiting for widget bootstrap and token refresh endpoints to prevent abuse.

## Operational Checklist
- [ ] Add widget bootstrap endpoint that returns tenant metadata (marketplace base URL, JWKS URL, feature flags).
- [ ] Implement refresh-token flow and JWKS publication for widget JWT validation.
- [ ] Persist and expose per-tenant marketplace routes to the widget.
- [ ] Create shared cart storage keyed by tenant + contact, with restore on login.
- [ ] Add contact linkage logic (phone/email/DNI) and point-award service.
- [ ] Enforce RBAC and tenant filters on cart, survey, and contact endpoints with regression tests.
- [ ] Instrument logs and metrics for token refresh failures and cross-channel cart merges.
