# Frontend handoff — Demo WhatsApp onboarding por rubro

## Objetivo

Cuando un admin crea/abre un tenant nuevo (municipio o pyme), debe poder activar demo en WhatsApp en 1 click y probar el menú según su rubro.

## Endpoint fuente

- `GET /auth/demo/catalog`
- `GET /api/v1/portal/:tenant_slug/integration` (tenant ya autenticado)
- `GET /api/public/widget-config?tenant=:slug` (widget web público)

## Nuevos bloques de contrato (JSON)

Dentro de `onboarding`:

- `twilio_trial`
  - `display_number`: `+1 (415) 523-8886`
  - `join_phrase`: `join brief-yesterday`
  - `wa_deeplink`: link para abrir WhatsApp con frase precompletada
  - `security_limits.messages_per_session`: `10`
  - `security_limits.upgrade_required_for`: lista de features bloqueadas en demo

- `menus_by_tipo`
  - `municipio`: quick actions (reclamos, sugerencias, estado ticket, encuestas, heatmap)
  - `pyme`: quick actions (catálogo, pedido, estado pedido, subir catálogo, consulta producto)

- `demo_feature_access`
  - flags de capacidades habilitadas en trial (audio, imagen, reclamos, pedidos, heatmap, carga PDF/XLSX, etc.)

En `GET /api/v1/portal/:tenant_slug/integration`:

- `demoOnboarding.twilio_trial`
- `demoOnboarding.activation_state`
- `demoOnboarding.activation_endpoint` (`POST /api/v1/portal/:tenant_slug/integration/demo/activate-whatsapp`)
- `demoOnboarding.quick_menu` (ya adaptado por tipo de tenant)
- `demoOnboarding.feature_flags`
- `demoOnboarding.experience_blueprint` (UI ready: hero + quick actions + journeys + upsell)
  - incluye `channel_playbooks.whatsapp` / `channel_playbooks.widget_chat` con mensajes de prueba y media checks
  - incluye `conversion_pitch` para CTA comercial
  - incluye `component_pack` con secciones/componentes sugeridos para render inmediato

En `GET /api/public/widget-config`:

- `widget.demo_trial`
- `widget.rubro_profile`
- `widget.quick_menu`
- `builder_config.quick_menu` (duplicado útil para UI builders)
- `widget.experience_blueprint` (contrato UI listo para render)

## Recomendación UX/UI (flujo)

1. **Paso sector/rubro** (ya existente).
2. **Paso WhatsApp demo**:
   - botón primario: “Activar demo en WhatsApp” (llamar `activation_endpoint` primero).
   - abrir `onboarding.twilio_trial.wa_deeplink`.
   - mostrar instrucción textual con número + frase exacta.
   - respetar `activation_state.max_activations = 1` por tenant.
3. **Paso probar menú inteligente**:
   - renderizar quick actions desde `menus_by_tipo[tipo_chat]`.
   - mostrar badge “Demo (10 mensajes)” usando `security_limits.messages_per_session`.
4. **Gating enterprise**:
   - al intentar features de `upgrade_required_for`, mostrar modal de upgrade.

## Señal comercial automática (Super Admin)

Cuando se activa demo WhatsApp por tenant:

- backend emite señal de `hot_lead` para super admins (notificación in-app),
- se registra auditoría `tenant_demo_whatsapp_activated`,
- esto permite seguimiento comercial inmediato del prospecto.

## Copy sugerido para upgrade

> “Llegaste al límite de demo. Activá plan Full para continuar con catálogos en Qdrant y automatizaciones avanzadas.”
