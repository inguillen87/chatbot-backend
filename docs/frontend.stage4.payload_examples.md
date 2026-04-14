# Frontend Stage 4 — Payload Examples (v1)

> Ejemplos de referencia para tipado y pruebas de integración FE.

## 1) GET `/analytics/identity/coverage`

### Response 200 (con alertas)

```json
{
  "contract_version": "analytics.identity_coverage.v1",
  "tenant_id": 42,
  "scope": "tenant",
  "target_pct": 90.0,
  "coverage_pct": 84.5,
  "slo_status": "below_target",
  "alert_count": 2,
  "alerts": [
    {
      "channel": "whatsapp",
      "coverage_pct": 81.0,
      "target_pct": 95.0,
      "gap_pct": 14.0,
      "severity": "high"
    },
    {
      "channel": "web",
      "coverage_pct": 88.0,
      "target_pct": 90.0,
      "gap_pct": 2.0,
      "severity": "low"
    }
  ],
  "channels": [
    {
      "channel": "whatsapp",
      "events": 810,
      "with_contact_key": 656,
      "coverage_pct": 81.0
    },
    {
      "channel": "web",
      "events": 500,
      "with_contact_key": 440,
      "coverage_pct": 88.0
    }
  ]
}
```

### Response 200 (sin alertas)

```json
{
  "contract_version": "analytics.identity_coverage.v1",
  "tenant_id": 42,
  "scope": "tenant",
  "target_pct": 85.0,
  "coverage_pct": 92.4,
  "slo_status": "ok",
  "alert_count": 0,
  "alerts": [],
  "channels": []
}
```

---

## 2) GET `/admin/analytics/whatsapp-funnel`

### Response 200

```json
{
  "tenant_id": 42,
  "scope": "tenant",
  "window_minutes": 60,
  "cutoff": "2026-04-14T12:00:00+00:00",
  "contract_version": "admin.analytics.whatsapp_funnel.v1",
  "stages": [
    {
      "event_name": "message_received",
      "label": "Mensaje recibido",
      "sessions": 300,
      "unique_contacts": 275,
      "conversion_from_prev_pct": null
    },
    {
      "event_name": "bot_replied",
      "label": "Bot respondió",
      "sessions": 255,
      "unique_contacts": 240,
      "conversion_from_prev_pct": 85.0
    },
    {
      "event_name": "human_handoff_requested",
      "label": "Pidió derivación",
      "sessions": 90,
      "unique_contacts": 86,
      "conversion_from_prev_pct": 35.3
    }
  ],
  "totals": {
    "events": 645,
    "unique_sessions": 300,
    "unique_contacts": 275
  }
}
```

---

## 3) Error estándar 403 (RBAC)

```json
{
  "error": {
    "code": 403,
    "message": "forbidden",
    "capability": "analytics.admin"
  }
}
```

---

## 4) POST `/analytics/event`

### Response 202

```json
{
  "ok": true,
  "contract_version": "analytics.event_ingest.v1",
  "tenant_id": 42,
  "event_name": "portal_opened",
  "contact_key": "wa:contact:abc123",
  "conversation_id": "conv-abc123",
  "identity_source": "conversation_id"
}
```

---

## 5) Notas para FE QA

- Validar que `contract_version` exista antes de renderizar vistas críticas.
- Soportar `conversion_from_prev_pct: null` en primera etapa del funnel.
- Tratar `alerts` vacío como estado sano (sin banner).

---

## 6) GET `/auth/widget/bootstrap`

### Response 200 (extracto)

```json
{
  "contract_version": "auth.widget_bootstrap.v1",
  "tenant": {
    "id": 42,
    "slug": "demo-tenant"
  },
  "widget": {
    "token_cookie_name": "widget_token",
    "access_minutes": 45,
    "renew_days": 7
  },
  "jwks": {
    "url": "https://api.chatboc.ar/auth/widget/jwks.json"
  }
}
```

---

## 7) POST `/auth/widget-token` y `/auth/widget-refresh`

### Response 200

```json
{
  "contract_version": "auth.widget_token.v1",
  "token": "<jwt>",
  "expires_in": 2700
}
```
