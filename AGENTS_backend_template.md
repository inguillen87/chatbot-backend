# AGENTS.md — backend

## Objetivo del repo
Backend multi-tenant omnicanal para conversaciones, tickets, notificaciones, voz, WhatsApp, analítica y administración.

## Reglas de trabajo
- No hacer refactors masivos.
- Priorizar cambios pequeños, reversibles y testeados.
- Todo endpoint nuevo debe validar tenant y permisos.
- Toda acción sensible debe generar audit log.
- Mantener compatibilidad con flujos legacy salvo instrucción explícita.
- No modificar contratos públicos sin documentar migración.

## Estándares
- Preferir servicios/adapters reutilizables sobre lógica en controladores.
- Idempotencia obligatoria en webhooks, streams y notificaciones.
- Tests obligatorios para lógica nueva.
- Si un cambio toca multitenancy, agregar test de aislamiento.

## Qué NO hacer
- No tocar frontend desde este repo.
- No inventar endpoints si ya existe contrato.
- No hardcodear tenant_id, permisos o estados.

## Definition of Done
- tests verdes
- migración clara
- logs/audit donde corresponde
- documentación breve del cambio
