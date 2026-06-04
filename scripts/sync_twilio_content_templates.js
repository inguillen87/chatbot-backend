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
 *   node scripts/sync_twilio_content_templates.js --approve
 *   node scripts/sync_twilio_content_templates.js --create --approve
 */

const fs = require("fs");
const os = require("os");
const path = require("path");

const DEFAULT_BASE_URL = process.env.CHATBOC_PUBLIC_BASE_URL || "https://www.chatboc.ar";
const DEFAULT_LANGUAGE = process.env.CHATBOC_TEMPLATE_LANGUAGE || "es";
const TEMPLATE_PREFIX = "chatboc";

const args = new Set(process.argv.slice(2));
const options = {
  dryRun: args.has("--dry-run") || (!args.has("--create") && !args.has("--approve") && !args.has("--status")),
  create: args.has("--create"),
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
    name: "chatboc_welcome_menu_v1",
    category: "UTILITY",
    body: "Hola {{1}}, soy {{2}}. Te ayudo con reclamos, pedidos, pagos, encuestas y soporte. Elegi una opcion para continuar.",
    variables: { "1": "Marcelo", "2": "Chatboc" },
    types: (body) => ({
      "twilio/text": { body },
      "twilio/quick-reply": {
        body,
        actions: [
          { type: "QUICK_REPLY", title: "Crear caso", id: "open_case" },
          { type: "QUICK_REPLY", title: "Ver pedido", id: "track_order" },
          { type: "QUICK_REPLY", title: "Hablar equipo", id: "human_handoff" },
        ],
      },
    }),
  },
  {
    name: "chatboc_order_checkout_v1",
    category: "UTILITY",
    body: "Tu pedido {{1}} esta listo. Total {{2}}. Paga de forma segura desde este enlace: {{3}}",
    variables: { "1": "PED-1001", "2": "$25.000", "3": `${DEFAULT_BASE_URL}/checkout/demo` },
    types: (body) => withCta(body, "Pagar seguro", "{{3}}"),
  },
  {
    name: "chatboc_payment_confirmed_v1",
    category: "UTILITY",
    body: "Pago acreditado para {{1}}. El pedido queda confirmado. Seguimiento: {{2}}",
    variables: { "1": "PED-1001", "2": `${DEFAULT_BASE_URL}/t/PED-1001` },
    types: (body) => withCta(body, "Ver seguimiento", "{{2}}"),
  },
  {
    name: "chatboc_school_payment_due_v1",
    category: "UTILITY",
    body: "{{1}}, tenes {{2}} pendiente por {{3}}. Podes pagarlo aca: {{4}}",
    variables: {
      "1": "Familia Perez",
      "2": "Cuota Mayo",
      "3": "$25.000",
      "4": `${DEFAULT_BASE_URL}/checkout/colegio-demo`,
    },
    types: (body) => withCta(body, "Pagar cuota", "{{4}}"),
  },
  {
    name: "chatboc_school_certificate_ready_v1",
    category: "UTILITY",
    body: "El certificado de {{1}} esta listo. Descargalo o seguilo aca: {{2}}",
    variables: { "1": "Juan Perez", "2": `${DEFAULT_BASE_URL}/t/CERT-1001` },
    types: (body) => withCta(body, "Ver certificado", "{{2}}"),
  },
  {
    name: "chatboc_gov_claim_created_v1",
    category: "UTILITY",
    body: "Tu reclamo {{1}} fue registrado. Categoria: {{2}}. Seguimiento: {{3}}",
    variables: {
      "1": "REC-1001",
      "2": "Luminarias",
      "3": `${DEFAULT_BASE_URL}/t/REC-1001`,
    },
    types: (body) => withCta(body, "Ver reclamo", "{{3}}"),
  },
  {
    name: "chatboc_survey_invite_v1",
    category: "UTILITY",
    body: "{{1}} te invita a responder: {{2}}. Participa aca: {{3}}",
    variables: {
      "1": "Chatboc",
      "2": "Encuesta de satisfaccion",
      "3": `${DEFAULT_BASE_URL}/e/demo`,
    },
    types: (body) => withCta(body, "Responder", "{{3}}"),
  },
  {
    name: "chatboc_handoff_v1",
    category: "UTILITY",
    body: "Derivamos tu consulta {{1}} al equipo. Un operador va a responderte por este canal.",
    variables: { "1": "CASO-1001" },
    types: (body) => ({
      "twilio/text": { body },
      "twilio/quick-reply": {
        body,
        actions: [
          { type: "QUICK_REPLY", title: "Ver caso", id: "view_case" },
          { type: "QUICK_REPLY", title: "Cancelar", id: "cancel" },
        ],
      },
    }),
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
        result.action = "created";
        result.sid = content.sid;
      } else if (!content) {
        result.action = "missing";
      } else {
        result.action = "exists";
      }

      if (content && (options.approve || options.status)) {
        result.approval = await getApprovalStatus(client, content.sid);
      }

      if (content && options.approve && !isApproved(result.approval)) {
        const approval = await client.content.v1.contents(content.sid).approvalCreate.create({
          name: definition.friendlyName,
          category: definition.category,
        });
        result.action = result.action === "created" ? "created_and_submitted" : "submitted";
        result.approval = normalizeApproval(approval);
      }
    } catch (error) {
      result.error = sanitizeError(error);
    }

    results.push(result);
  }

  writeJson({
    ok: results.every((result) => !result.error),
    mode: modeLabel(),
    accountSid: maskSid(auth.accountSid),
    count: results.length,
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
  const contents = await client.content.v1.contents.list({ limit: 1000 });
  for (const content of contents) {
    if (content.friendlyName && !map.has(content.friendlyName)) {
      map.set(content.friendlyName, content);
    }
  }
  return map;
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
  if (options.approve) modes.push("approve");
  return modes.join("+") || "noop";
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
