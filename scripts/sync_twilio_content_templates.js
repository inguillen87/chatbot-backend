#!/usr/bin/env node
"use strict";

/*
 * Sync Chatboc WhatsApp Content API templates with Twilio.
 *
 * This script intentionally does not add backend endpoints. It creates and
 * submits reusable Twilio Content templates for Meta WhatsApp approval.
 *
 * Usage:
 *   node scripts/sync_twilio_content_templates.js --dry-run
 *   node scripts/sync_twilio_content_templates.js --status
 *   node scripts/sync_twilio_content_templates.js --create
 *   node scripts/sync_twilio_content_templates.js --update
 *   node scripts/sync_twilio_content_templates.js --approve
 *   node scripts/sync_twilio_content_templates.js --create --approve
 */

const fs = require("fs");
const os = require("os");
const path = require("path");

const DEFAULT_BASE_URL = process.env.CHATBOC_PUBLIC_BASE_URL || "https://www.chatboc.ar";
const DEFAULT_LANGUAGE = process.env.CHATBOC_TEMPLATE_LANGUAGE || "es";
const TEMPLATE_PREFIX = "chatboc";
const MANIFEST_PATH = path.join(__dirname, "twilio_content_templates.local.json");

const existingOperationalTemplates = [
  { name: "gobiernos_reclamo_sla", vertical: "gobierno", stage: "claim", entrypoint: "whatsapp" },
  { name: "gobiernos_tasa_vencimiento", vertical: "gobierno", stage: "payment", entrypoint: "webview" },
  { name: "gobiernos_turno_confirmacion", vertical: "gobierno", stage: "appointment", entrypoint: "whatsapp" },
  { name: "gobiernos_comunicado_segmentado", vertical: "gobierno", stage: "announcement", entrypoint: "webview" },
  { name: "gobiernos_tramite_estado", vertical: "gobierno", stage: "procedure", entrypoint: "whatsapp" },
  { name: "colegios_cuota_vencimiento", vertical: "colegio", stage: "payment", entrypoint: "webview" },
  { name: "colegios_turno_administracion", vertical: "colegio", stage: "appointment", entrypoint: "whatsapp" },
  { name: "clinicas_turno_recordatorio", vertical: "clinica", stage: "appointment", entrypoint: "whatsapp" },
  { name: "clubes_cuota_social", vertical: "club", stage: "payment", entrypoint: "webview" },
  { name: "clubes_reserva_disciplina", vertical: "club", stage: "reservation", entrypoint: "whatsapp" },
  { name: "consorcios_expensas_vencimiento", vertical: "consorcio", stage: "payment", entrypoint: "webview" },
  { name: "consorcios_reclamo_sla", vertical: "consorcio", stage: "claim", entrypoint: "whatsapp" },
  { name: "consorcios_comprobante_recibido", vertical: "consorcio", stage: "receipt", entrypoint: "whatsapp" },
  { name: "consorcios_reserva_amenity", vertical: "consorcio", stage: "reservation", entrypoint: "whatsapp" },
  { name: "emprendedores_pedido_confirmado", vertical: "pyme", stage: "order", entrypoint: "whatsapp" },
  { name: "emprendedores_comprobante_revision", vertical: "pyme", stage: "receipt", entrypoint: "whatsapp" },
  { name: "standard_handoff_humano", vertical: "all", stage: "handoff", entrypoint: "whatsapp" },
  { name: "message_opt_in", vertical: "all", stage: "consent", entrypoint: "whatsapp" },
  { name: "copy_navegacion", vertical: "all", stage: "menu", entrypoint: "whatsapp" },
  { name: "customer_care_greeting_template", vertical: "all", stage: "support", entrypoint: "whatsapp" },
  { name: "customer_care_help_center_template", vertical: "all", stage: "support", entrypoint: "webview" },
  { name: "customer_support_routing_template", vertical: "all", stage: "support", entrypoint: "whatsapp" },
  { name: "notification_order_tracking", vertical: "pyme", stage: "tracking", entrypoint: "whatsapp" },
  { name: "promocionar", vertical: "all", stage: "marketing", entrypoint: "whatsapp" },
  { name: "bannerencu", vertical: "all", stage: "survey", entrypoint: "whatsapp" },
  { name: "saludo_inicial_juni", vertical: "gobierno", stage: "media_welcome", entrypoint: "whatsapp" },
  { name: "turnero_pago_seguro_webview", vertical: "all", stage: "payment", entrypoint: "webview" },
  { name: "turnero_pyme_pedido_webview", vertical: "pyme", stage: "order", entrypoint: "webview" },
  { name: "turnero_colegio_admision_webview", vertical: "colegio", stage: "admission", entrypoint: "webview" },
  { name: "turnero_gobierno_tramite_webview", vertical: "gobierno", stage: "procedure", entrypoint: "webview" },
  { name: "turnero_soporte_caso_webview", vertical: "all", stage: "support", entrypoint: "webview" },
];

const args = new Set(process.argv.slice(2));
const options = {
  dryRun: args.has("--dry-run") ||
    (!args.has("--create") && !args.has("--update") && !args.has("--approve") && !args.has("--status")),
  create: args.has("--create"),
  update: args.has("--update"),
  approve: args.has("--approve"),
  status: args.has("--status"),
  json: args.has("--json") || true,
  only: readArgValue("--only"),
};

const onlyNames = options.only
  ? new Set(options.only.split(",").map((name) => name.trim()).filter(Boolean))
  : null;

const templates = [
  {
    name: "chatboc_welcome_menu_v2",
    category: "UTILITY",
    body: "Hola {{1}}, soy {{2}}. Te ayudo por WhatsApp con reclamos, pedidos, pagos, certificados, encuestas y derivacion a equipo. Elegi una opcion.",
    variables: { "1": "Marcelo", "2": "Chatboc" },
    types: (body) => withQuickReplies(body, [
      { title: "Crear caso", id: "open_case" },
      { title: "Pagar o pedir", id: "commerce" },
      { title: "Hablar equipo", id: "human_handoff" },
    ]),
  },
  {
    name: "chatboc_order_checkout_v1",
    category: "UTILITY",
    body: "Tu pedido {{1}} esta listo. Total {{2}}. Paga de forma segura desde el boton de pago.",
    variables: { "1": "PED-1001", "2": "$25.000", "3": "demo" },
    types: (body) => withCta(body, "Pagar seguro", `${DEFAULT_BASE_URL}/checkout/{{3}}`),
  },
  {
    name: "chatboc_payment_confirmed_v1",
    category: "UTILITY",
    body: "Pago acreditado para {{1}}. El pedido queda confirmado. Usa el boton para ver seguimiento.",
    variables: { "1": "PED-1001", "2": "PED-1001" },
    types: (body) => withCta(body, "Ver seguimiento", `${DEFAULT_BASE_URL}/t/{{2}}`),
  },
  {
    name: "chatboc_school_payment_due_v2",
    category: "UTILITY",
    body: "Hola {{1}}, hay una cuota escolar pendiente registrada en Chatboc. Para ver el detalle y pagar de forma segura, usa el boton de pago. Si ya pagaste, responde con el comprobante.",
    variables: {
      "1": "Familia Perez",
      "2": "colegio-demo",
    },
    types: (body) => withCta(body, "Pagar cuota", `${DEFAULT_BASE_URL}/checkout/{{2}}`),
  },
  {
    name: "chatboc_school_certificate_ready_v1",
    category: "UTILITY",
    body: "El certificado de {{1}} esta listo. Usa el boton para descargarlo o seguirlo.",
    variables: { "1": "Juan Perez", "2": "CERT-1001" },
    types: (body) => withCta(body, "Ver certificado", `${DEFAULT_BASE_URL}/t/{{2}}`),
  },
  {
    name: "chatboc_gov_claim_created_v2",
    category: "UTILITY",
    body: "Registramos tu reclamo municipal {{1}}. Ya quedo derivado al area correspondiente. Podes consultar el estado y agregar informacion desde el boton de seguimiento.",
    variables: {
      "1": "REC-1001",
      "2": "REC-1001",
    },
    types: (body) => withCta(body, "Ver reclamo", `${DEFAULT_BASE_URL}/t/{{2}}`),
  },
  {
    name: "chatboc_survey_invite_v2",
    category: "UTILITY",
    body: "Te invitamos a responder una encuesta de {{1}}. Tu participacion ayuda a mejorar la atencion y ver resultados agregados despues de votar.",
    variables: {
      "1": "Chatboc",
      "2": "demo",
    },
    types: (body) => withCta(body, "Responder", `${DEFAULT_BASE_URL}/e/{{2}}`),
  },
  {
    name: "chatboc_handoff_v1",
    category: "UTILITY",
    body: "Derivamos tu consulta {{1}} al equipo. Un operador va a responderte por este canal.",
    variables: { "1": "CASO-1001" },
    types: (body) => withQuickReplies(body, [
      { title: "Ver caso", id: "view_case" },
      { title: "Cancelar", id: "cancel" },
    ]),
  },
  {
    name: "chatboc_pyme_order_ready_v1",
    category: "UTILITY",
    body: "Tu pedido {{1}} esta listo. Total {{2}}. Revisalo y pagalo desde el boton seguro.",
    variables: { "1": "PED-1001", "2": "$25.000", "3": "PED-1001" },
    types: (body) => withCta(body, "Pagar pedido", `${DEFAULT_BASE_URL}/checkout/{{3}}`),
  },
  {
    name: "chatboc_pyme_payment_link_v1",
    category: "UTILITY",
    body: "Hola {{1}}, tu link de pago por {{2}} esta disponible. No compartas datos de tarjeta por chat.",
    variables: { "1": "Marcelo", "2": "$25.000", "3": "PAY-1001" },
    types: (body) => withCta(body, "Pagar seguro", `${DEFAULT_BASE_URL}/checkout/{{3}}`),
  },
  {
    name: "chatboc_pyme_delivery_update_v1",
    category: "UTILITY",
    body: "Actualizacion del pedido {{1}}: {{2}}. Podes ver el seguimiento desde el boton.",
    variables: { "1": "PED-1001", "2": "en preparacion", "3": "PED-1001" },
    types: (body) => withCta(body, "Ver seguimiento", `${DEFAULT_BASE_URL}/t/{{3}}`),
  },
  {
    name: "chatboc_pyme_quote_followup_v1",
    category: "UTILITY",
    body: "Tu cotizacion {{1}} ya esta preparada. Revisala y confirma si queres avanzar.",
    variables: { "1": "COT-1001", "2": "COT-1001" },
    types: (body) => withCta(body, "Ver cotizacion", `${DEFAULT_BASE_URL}/t/{{2}}`),
  },
  {
    name: "chatboc_pyme_catalog_invite_v1",
    category: "UTILITY",
    body: "Mira el catalogo actualizado de {{1}}. Podes consultar productos y armar pedido desde el boton.",
    variables: { "1": "Ferreteria demo", "2": "catalogo-demo" },
    types: (body) => withCta(body, "Ver catalogo", `${DEFAULT_BASE_URL}/catalogo/{{2}}`),
  },
  {
    name: "chatboc_school_receipt_ready_v1",
    category: "UTILITY",
    body: "Comprobante {{1}} disponible para {{2}}. Podes descargarlo desde el boton.",
    variables: { "1": "REC-1001", "2": "Juan Perez", "3": "REC-1001" },
    types: (body) => withCta(body, "Ver comprobante", `${DEFAULT_BASE_URL}/t/{{3}}`),
  },
  {
    name: "chatboc_school_family_case_created_v1",
    category: "UTILITY",
    body: "Creamos el caso {{1}} para {{2}}. Estado: {{3}}. El equipo puede continuar por este canal.",
    variables: { "1": "ESC-1001", "2": "Juan Perez", "3": "recibido" },
    types: (body) => withQuickReplies(body, [
      { title: "Ver caso", id: "view_case" },
      { title: "Adjuntar info", id: "attach_info" },
      { title: "Hablar equipo", id: "human_handoff" },
    ]),
  },
  {
    name: "chatboc_school_event_reminder_v1",
    category: "UTILITY",
    body: "Recordatorio de {{1}}: {{2}}. Revisa la informacion completa desde el boton.",
    variables: { "1": "reunion escolar", "2": "viernes 10 hs", "3": "EVT-1001" },
    types: (body) => withCta(body, "Ver evento", `${DEFAULT_BASE_URL}/t/{{3}}`),
  },
  {
    name: "chatboc_gov_claim_status_update_v1",
    category: "UTILITY",
    body: "Actualizacion del reclamo {{1}}: {{2}}. Podes ver el detalle desde el boton.",
    variables: { "1": "REC-1001", "2": "en revision", "3": "REC-1001" },
    types: (body) => withCta(body, "Ver reclamo", `${DEFAULT_BASE_URL}/t/{{3}}`),
  },
  {
    name: "chatboc_gov_turn_reminder_v1",
    category: "UTILITY",
    body: "Recordatorio: turno {{1}} para {{2}} el {{3}}. Revisa el detalle desde el boton.",
    variables: { "1": "TUR-1001", "2": "Mesa de entradas", "3": "viernes 10 hs", "4": "TUR-1001" },
    types: (body) => withCta(body, "Ver turno", `${DEFAULT_BASE_URL}/t/{{4}}`),
  },
  {
    name: "chatboc_gov_document_ready_v1",
    category: "UTILITY",
    body: "Tu documento {{1}} esta listo. Podes consultarlo o descargarlo desde el boton.",
    variables: { "1": "DOC-1001", "2": "DOC-1001" },
    types: (body) => withCta(body, "Ver documento", `${DEFAULT_BASE_URL}/t/{{2}}`),
  },
  {
    name: "chatboc_gov_survey_invite_v1",
    category: "UTILITY",
    body: "{{1}} te invita a participar: {{2}}. Responde desde el boton y mira resultados agregados.",
    variables: { "1": "Municipio demo", "2": "encuesta ciudadana", "3": "encuesta-demo" },
    types: (body) => withCta(body, "Responder", `${DEFAULT_BASE_URL}/e/{{3}}`),
  },
].filter((template) => !onlyNames || onlyNames.has(template.name));

main().catch((error) => {
  writeJson({
    ok: false,
    error: sanitizeError(error),
  });
  process.exitCode = 1;
});

async function main() {
  const validation = validateTemplates(templates);
  if (validation.errors.length > 0) {
    writeJson({ ok: false, mode: "validate", errors: validation.errors });
    process.exitCode = 1;
    return;
  }

  const definitions = templates.map(toContentDefinition);

  if (options.dryRun) {
    writeJson({
      ok: true,
      mode: "dry-run",
      count: definitions.length,
      templates: definitions.map(publicDefinition),
    });
    return;
  }

  const twilio = loadTwilioSdk();
  const auth = loadTwilioAuth();
  const client = buildTwilioClient(twilio, auth);

  const existing = await listContentByFriendlyName(client);
  const manifest = loadManifest();
  mergeManifest(existing, manifest);
  const results = [];

  for (const definition of definitions) {
    let content = existing.get(definition.friendlyName);
    const result = {
      name: definition.friendlyName,
      category: definition.category,
      action: "none",
      sid: content ? content.sid : null,
      approval: null,
      error: null,
    };

    try {
      if (!content && options.create) {
        content = await client.content.v1.contents.create({
          friendlyName: definition.friendlyName,
          language: definition.language,
          variables: definition.variables,
          types: definition.types,
        });
        existing.set(definition.friendlyName, content);
        manifest.templates[definition.friendlyName] = {
          sid: content.sid,
          category: definition.category,
          language: definition.language,
          updatedAt: new Date().toISOString(),
        };
        result.action = "created";
        result.sid = content.sid;
      } else if (!content) {
        result.action = "missing";
      } else {
        result.action = "exists";
      }

      if (content && (options.approve || options.status || options.update)) {
        result.approval = await getApprovalStatus(client, content.sid);
      }

      if (content && options.update && canUpdateContent(result.approval)) {
        content = await client.content.v1.contents(content.sid).update({
          friendlyName: definition.friendlyName,
          language: definition.language,
          variables: definition.variables,
          types: definition.types,
        });
        result.action = result.action === "created" ? "created_and_updated" : "updated";
        result.sid = content.sid;
        result.approval = await getApprovalStatus(client, content.sid);
      }

      if (content && options.approve && needsApprovalSubmission(result.approval)) {
        const approval = await client.content.v1.contents(content.sid).approvalCreate.create({
          name: definition.friendlyName,
          category: definition.category,
        });
        result.action = result.action === "created" ? "created_and_submitted" : "submitted";
        result.approval = normalizeApproval(approval);
      }

      if (content) {
        const existingManifestEntry = manifest.templates[definition.friendlyName] || {};
        const normalizedApprovalStatus = approvalStatus(result.approval);
        manifest.templates[definition.friendlyName] = {
          ...existingManifestEntry,
          sid: content.sid,
          category: definition.category,
          language: definition.language,
          approvalStatus: normalizedApprovalStatus || existingManifestEntry.approvalStatus || null,
          approved: normalizedApprovalStatus
            ? isApproved(result.approval)
            : Boolean(existingManifestEntry.approved),
          lastStatusAt: (options.status || options.approve || options.update)
            ? new Date().toISOString()
            : existingManifestEntry.lastStatusAt,
          updatedAt: new Date().toISOString(),
        };
      }
    } catch (error) {
      result.error = sanitizeError(error);
    }

    results.push(result);
  }

  saveManifest(manifest);

  writeJson({
    ok: results.every((result) => !result.error),
    mode: modeLabel(),
    accountSid: maskSid(auth.accountSid),
    count: results.length,
    catalog: buildCatalogSummary(existing),
    results,
  });
}

function withCta(body, title, url) {
  return {
    "twilio/text": { body },
    "twilio/call-to-action": {
      body,
      actions: [{ type: "URL", title, url }],
    },
  };
}

function withQuickReplies(body, actions) {
  return {
    "twilio/text": { body },
    "twilio/quick-reply": {
      body,
      actions: actions.map((action) => ({
        type: "QUICK_REPLY",
        title: action.title,
        id: action.id,
      })),
    },
  };
}

function toContentDefinition(template) {
  return {
    friendlyName: template.name,
    category: template.category,
    language: DEFAULT_LANGUAGE,
    variables: template.variables,
    types: template.types(template.body),
  };
}

function publicDefinition(definition) {
  return {
    friendlyName: definition.friendlyName,
    category: definition.category,
    language: definition.language,
    variables: definition.variables,
    types: Object.keys(definition.types),
  };
}

function validateTemplates(items) {
  const errors = [];
  const names = new Set();

  for (const item of items) {
    if (!new RegExp(`^${TEMPLATE_PREFIX}_[a-z0-9_]+_v[0-9]+$`).test(item.name)) {
      errors.push(`${item.name}: invalid Meta-compatible name`);
    }
    if (names.has(item.name)) {
      errors.push(`${item.name}: duplicated template name`);
    }
    names.add(item.name);

    const variableNumbers = [...item.body.matchAll(/\{\{(\d+)\}\}/g)].map((match) => Number(match[1]));
    const uniqueVariables = [...new Set(variableNumbers)].sort((a, b) => a - b);
    uniqueVariables.forEach((value, index) => {
      if (value !== index + 1) errors.push(`${item.name}: variables must be sequential from {{1}}`);
    });
    for (const value of uniqueVariables) {
      if (!Object.prototype.hasOwnProperty.call(item.variables, String(value))) {
        errors.push(`${item.name}: missing sample for {{${value}}}`);
      }
    }

    const types = item.types(item.body);
    for (const [type, payload] of Object.entries(types)) {
      if (!payload || typeof payload.body !== "string" || payload.body.length < 10) {
        errors.push(`${item.name}: ${type} needs a readable body`);
      }
      if (payload.actions) {
        for (const action of payload.actions) {
          if (!action.title || action.title.length > 20) {
            errors.push(`${item.name}: button title must be 1-20 chars`);
          }
        }
      }
    }
  }

  return { errors };
}

async function listContentByFriendlyName(client) {
  const map = new Map();
  const page = await client.content.v1.contents.page({ pageSize: 1000 });
  for (const content of page.instances || []) {
    const friendlyName = contentFriendlyName(content);
    if (friendlyName && !map.has(friendlyName)) {
      map.set(friendlyName, content);
    }
  }
  return map;
}

function contentFriendlyName(content) {
  if (!content) return null;
  if (content.friendlyName) return content.friendlyName;
  if (content.friendly_name) return content.friendly_name;
  const json = typeof content.toJSON === "function" ? content.toJSON() : content;
  return json.friendly_name || json.friendlyName || null;
}

function buildCatalogSummary(existing) {
  const managed = templates.map((definition) => ({
    name: definition.name,
    sid: existing.get(definition.name) ? existing.get(definition.name).sid : null,
    status: existing.has(definition.name) ? "present" : "missing",
    managed: true,
  }));
  const operational = existingOperationalTemplates.map((definition) => ({
    ...definition,
    sid: existing.get(definition.name) ? existing.get(definition.name).sid : null,
    status: existing.has(definition.name) ? "present" : "missing",
    managed: false,
  }));
  return {
    totalNamedInAccount: existing.size,
    managedTemplates: managed.length,
    managedPresent: managed.filter((item) => item.sid).length,
    operationalTemplates: operational.length,
    operationalPresent: operational.filter((item) => item.sid).length,
    templates: [...managed, ...operational],
  };
}

async function getApprovalStatus(client, sid) {
  try {
    const approval = await client.content.v1.contents(sid).approvalFetch().fetch();
    return normalizeApproval(approval);
  } catch (error) {
    const message = String(error && error.message ? error.message : error);
    if (message.includes("not found") || message.includes("404")) {
      return { status: "NOT_SUBMITTED" };
    }
    return { status: "UNKNOWN", error: sanitizeError(error) };
  }
}

function normalizeApproval(approval) {
  if (!approval) return null;
  const json = typeof approval.toJSON === "function" ? approval.toJSON() : approval;
  return {
    status: json.status || (json.whatsapp && json.whatsapp.status) || "UNKNOWN",
    category: json.category || (json.whatsapp && json.whatsapp.category) || null,
    rejectionReason: json.rejectionReason || (json.whatsapp && json.whatsapp.rejection_reason) || null,
    whatsapp: json.whatsapp || null,
  };
}

function isApproved(approval) {
  const status = approval && String(approval.status || "").toUpperCase();
  return status === "APPROVED";
}

function loadTwilioSdk() {
  const candidates = [
    "twilio",
    process.env.TWILIO_SDK_PATH,
    path.join(
      process.env.LOCALAPPDATA || "",
      "Microsoft",
      "WinGet",
      "Packages",
      "OpenJS.NodeJS.LTS_Microsoft.Winget.Source_8wekyb3d8bbwe",
      "node-v24.15.0-win-x64",
      "node_modules",
      "twilio-cli",
      "node_modules",
      "twilio"
    ),
  ].filter(Boolean);

  const attempted = [];
  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch (error) {
      attempted.push(candidate);
    }
  }
  throw new Error(`Twilio SDK not found. Tried: ${attempted.join(", ")}`);
}

function loadManifest() {
  if (!fs.existsSync(MANIFEST_PATH)) {
    return { version: 1, templates: {} };
  }
  try {
    const parsed = JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf8"));
    return {
      version: parsed.version || 1,
      templates: parsed.templates || {},
    };
  } catch (_error) {
    return { version: 1, templates: {} };
  }
}

function mergeManifest(existing, manifest) {
  for (const [friendlyName, entry] of Object.entries(manifest.templates || {})) {
    if (!entry || !entry.sid || existing.has(friendlyName)) continue;
    existing.set(friendlyName, {
      sid: entry.sid,
      friendlyName,
      language: entry.language || DEFAULT_LANGUAGE,
    });
  }
}

function saveManifest(manifest) {
  const payload = {
    version: manifest.version || 1,
    templates: manifest.templates || {},
  };
  fs.writeFileSync(MANIFEST_PATH, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
}

function loadTwilioAuth() {
  const envAuth = {
    accountSid: process.env.TWILIO_ACCOUNT_SID,
    authToken: process.env.TWILIO_AUTH_TOKEN,
    apiKey: process.env.TWILIO_API_KEY,
    apiSecret: process.env.TWILIO_API_SECRET,
  };
  if (envAuth.accountSid && (envAuth.authToken || (envAuth.apiKey && envAuth.apiSecret))) {
    return envAuth;
  }

  const configPath = process.env.TWILIO_CLI_CONFIG ||
    path.join(os.homedir(), ".twilio-cli", "config.json");
  if (!fs.existsSync(configPath)) {
    throw new Error("Twilio credentials not found in environment or Twilio CLI profile");
  }

  const raw = JSON.parse(fs.readFileSync(configPath, "utf8"));
  const activeProfileName = raw.activeProfile || raw.profile || raw.defaultProfile;
  const profiles = raw.profiles || {};
  const profile = activeProfileName ? profiles[activeProfileName] : Object.values(profiles)[0];

  if (!profile) {
    throw new Error("No active Twilio CLI profile found");
  }

  return {
    accountSid: profile.accountSid || profile.account_sid,
    apiKey: profile.apiKey || profile.api_key,
    apiSecret: profile.apiSecret || profile.api_secret,
    authToken: profile.authToken || profile.auth_token,
    profileName: activeProfileName || "default",
  };
}

function buildTwilioClient(twilio, auth) {
  if (!auth.accountSid) {
    throw new Error("Twilio accountSid is required");
  }
  if (auth.apiKey && auth.apiSecret) {
    return twilio(auth.apiKey, auth.apiSecret, { accountSid: auth.accountSid });
  }
  if (auth.authToken) {
    return twilio(auth.accountSid, auth.authToken);
  }
  throw new Error("Twilio auth requires auth token or API key/secret");
}

function readArgValue(flag) {
  const prefix = `${flag}=`;
  for (const arg of process.argv.slice(2)) {
    if (arg.startsWith(prefix)) return arg.slice(prefix.length);
  }
  const index = process.argv.indexOf(flag);
  if (index >= 0 && process.argv[index + 1]) return process.argv[index + 1];
  return null;
}

function modeLabel() {
  const modes = [];
  if (options.status) modes.push("status");
  if (options.create) modes.push("create");
  if (options.update) modes.push("update");
  if (options.approve) modes.push("approve");
  return modes.join("+") || "noop";
}

function canUpdateContent(approval) {
  const status = approvalStatus(approval);
  return status === "UNSUBMITTED" || status === "NOT_SUBMITTED" || status === "UNKNOWN";
}

function needsApprovalSubmission(approval) {
  const status = approvalStatus(approval);
  return status === "UNSUBMITTED" || status === "NOT_SUBMITTED" || status === "UNKNOWN";
}

function approvalStatus(approval) {
  return String((approval && approval.status) || "").toUpperCase();
}

function maskSid(value) {
  if (!value) return null;
  return `${value.slice(0, 4)}...${value.slice(-4)}`;
}

function sanitizeError(error) {
  const raw = String(error && error.message ? error.message : error);
  return raw
    .replace(/AC[a-f0-9]{32}/gi, "AC***")
    .replace(/SK[a-f0-9]{32}/gi, "SK***")
    .replace(/HX[a-f0-9]{32}/gi, "HX***")
    .replace(/[a-f0-9]{32,}/gi, "[redacted]");
}

function writeJson(payload) {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
}
