# Identidad canónica de sesiones por tenant y canal

## Objetivo

`channel.session_identity.v1` reemplaza la continuidad basada en identificadores
globales que incluían teléfono (`whatsapp_{owner}_{phone}`). El binding durable
usa exclusivamente:

- `tenant_id`
- `channel` (`whatsapp` o `voice`)
- `provider` (`twilio`)
- `identity_version`
- `identity_hmac` (HMAC-SHA256, nunca el teléfono)
- un `chat_session_id` UUID aleatorio

La identidad normalizada sólo existe en memoria durante el HMAC. El binding,
los logs y los metadatos de cuarentena no guardan el teléfono ni el ID legacy.
`ChatSessionContext` puede seguir conteniendo datos de contacto necesarios para
el flujo de negocio y queda sujeto a su política de privacidad/retención; no se
usa como clave global.

## Invariantes

1. Un binding es único por `tenant + canal + proveedor + versión + HMAC`.
2. El mismo número en dos tenants produce HMAC y sesión diferentes.
3. Una sesión legacy se adopta sólo si hay exactamente una candidata cuyo
   `anon_id`, owner y tenant coinciden. Para filas sin `tenant_id`, el owner debe
   pertenecer inequívocamente al tenant.
4. Cero candidatas crea contexto nuevo. Dos o más candidatas crean contexto
   nuevo aislado y marcan `continuity_status=isolated`, sin borrar las anteriores
   ni perder el mensaje entrante.
5. Un turno WhatsApp durable persiste binding, versión, HMAC y sesión. El worker
   revalida los cuatro antes de reingresar al webhook. Un conflicto deja el turno
   y su payload en estado `dead` para revisión; nunca salta a otra sesión.
6. Voz sólo reutiliza contexto WhatsApp cuando el binding corresponde al mismo
   tenant/proveedor/HMAC. Un `chat_session_id` recibido en el envelope no otorga
   por sí solo acceso a esa sesión.

## Configuración y rollout

Variables:

```text
CHANNEL_SESSION_IDENTITY_MODE=legacy|shadow|enforce
CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1=<secreto aleatorio de 32+ bytes>
CHANNEL_SESSION_IDENTITY_VERSION_V1=v1
```

- `legacy`: rollback temporal y **no certificado**; conserva exclusivamente en
  el webhook síncrono el identificador histórico
  `whatsapp_{owner}_{phone}`, que incorpora PII en la clave. No habilitar queue
  ni voz realtime en producción con este modo y no usarlo como estado estable.
- `shadow`: crea y utiliza bindings canónicos para tráfico nuevo, adopta legacy
  sólo bajo las reglas estrictas y admite turns antiguos sin snapshot de binding.
  Puede usar transitoriamente `WHATSAPP_INBOUND_HASH_SECRET` o `SECRET_KEY` si
  tienen 32+ bytes, aunque se recomienda el secreto dedicado. No ejecuta el
  fallback PII de `legacy`.
- `enforce`: exige secreto dedicado. Todo turno durable debe traer snapshot de
  binding verificable. Producción exige este modo para
  `WHATSAPP_INBOUND_DURABILITY_MODE=queue` y para voice consent/realtime activo;
  es el objetivo final de producción y nunca reconstruye IDs desde el teléfono.

Secuencia recomendada:

1. Aplicar migración `20260730_channel_session_identity_v1`.
2. Configurar el secreto dedicado en web y workers, sin exponerlo en logs.
3. Activar `shadow` y observar outcomes `created`, `adopted`, `existing` e
   `isolated` por tenant. Revisar toda tasa de `isolated` inesperada.
4. Confirmar replay de cola y voz en staging con dos tenants que comparten el
   mismo número de prueba.
5. Cambiar a `enforce`; recién entonces habilitar queue/voz en producción.

## Rotación del secreto

No reemplazar el secreto manteniendo `v1`: produciría identidades nuevas y
pérdida de continuidad. Para rotar:

1. definir una nueva versión (`v2`) y soporte de lectura dual en una migración
   posterior;
2. crear bindings `v2` asociados a las sesiones verificadas;
3. cambiar escritura a `v2`;
4. retirar `v1` sólo al terminar la ventana de replay/retención.

Esta primera versión falla cerrada en `enforce` si cambia el secreto sin ese
procedimiento.

## Incidentes y cuarentena

- `legacy_membership_ambiguous`: no unir manualmente hasta revisar tenant,
  owner y consentimientos. El mensaje nuevo ya quedó preservado en contexto
  aislado o en el turno durable.
- `binding_context_scope_conflict`: tratar como posible corrupción/tampering.
  El servicio reubica tráfico nuevo en una sesión segura y registra generación,
  código y HMAC del ID anterior; el replay histórico se rechaza.
- `session_identity_replay_scope_conflict`: no reintentar a ciegas. Mantener el
  turno `dead`, verificar binding/tenant y resolver mediante operación auditada.
- Nunca copiar `context_data` entre sesiones para “arreglar” una cuarentena sin
  evidencia de pertenencia y autorización del tenant.

## Validación local vs. certificación externa

Los tests cubren HMAC, aislamiento multi-tenant, adopción legacy, cuarentena,
webhook síncrono, ingreso en cola, replay worker y continuidad de voz. Esto no
certifica todavía un deploy ni un webhook real de Twilio; staging debe probar la
migración en PostgreSQL, web/worker con el mismo secreto y callbacks reales antes
de declarar el rollout completo.
