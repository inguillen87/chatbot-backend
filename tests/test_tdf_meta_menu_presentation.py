"""Pure executed menu contracts; no app, DB, credential or provider imports.

The source definitions run with synthetic identity/storage reads and an explicit
selector result. Canonical normalize/materialize, routing, presentation and Meta
payload validators execute unchanged. Import removal prevents application boot.
"""
import ast
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import socket
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]


class NoApplicationImports(ast.NodeTransformer):
    def visit_Import(self, node):
        return ast.copy_location(ast.Pass(), node)

    def visit_ImportFrom(self, node):
        return ast.copy_location(ast.Pass(), node)


def definitions(path, namespace, names=None):
    source = REPO / path
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    tree.body = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Assign))
                 and (names is None or getattr(node, "name", None) in names)]
    tree = NoApplicationImports().visit(tree)
    ast.fix_missing_locations(tree)
    exec(compile(tree, str(source), "exec"), namespace)
    return namespace


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 11, 6, 19, tzinfo=timezone.utc)
        return value if tz else value.replace(tzinfo=None)


def fixture(*, counts=(4, 4, 3), text_length=30, label_length=18,
            visibility="public", review_status="reviewed", links=False):
    content = definitions("services/institutional_assistant_content.py", {
        "deepcopy": deepcopy, "datetime": FixedDatetime, "timezone": timezone,
        "hashlib": hashlib, "ipaddress": ipaddress, "json": json, "re": re,
        "urlsplit": urlsplit, "secure_filename": lambda name: "synthetic-document",
    })
    raw = {
        "contract_version": content["BUNDLE_CONTRACT"], "version": "synthetic-menu-regression",
        "tenant": {"id": 46, "slug": "tierra-del-fuego"}, "start": "start",
        "sources": {"s": {"id": "s", "title": "Synthetic public reference", "sha256": "a" * 64,
                           "page_count": 1, "document_visibility": visibility,
                           "review_status": review_status}},
        "nodes": {}, "node_evidence": {},
        "policy": {key: False for key in ("accepts_personal_data", "creates_real_cases",
                   "queries_official_records", "sends_notifications", "stores_feedback")},
    }
    selected = ["start"] if len(counts) == 1 else ["topic" + str(i) for i in range(1, len(counts) + 1)]
    raw["nodes"]["start"] = {"id": "start", "title": "Synthetic start", "text": "x" * text_length,
                               "actions": []}
    if len(counts) > 1:
        raw["nodes"]["start"]["actions"] = [
            {"code": str(i), "label": "Tema " + str(i), "target": node_id}
            for i, node_id in enumerate(selected, 1)]
    labels, destinations, index = [], [], 0
    for node_id, count in zip(selected, counts):
        if node_id != "start":
            raw["nodes"][node_id] = {"id": node_id, "title": "Synthetic " + node_id,
                                     "text": "x" * text_length, "actions": []}
        for code in range(1, count + 1):
            index += 1
            target = "destination" + str(index)
            label = "Destino " + str(index).zfill(2) + " " + "l" * max(0, label_length - 11)
            labels.append(label); destinations.append(target)
            raw["nodes"][node_id]["actions"].append({"code": str(code), "label": label, "target": target})
            raw["nodes"][target] = {"id": target, "title": "Synthetic target " + str(index),
                                     "text": "Destino elegido " + str(index), "actions": []}
    for node_id in raw["nodes"]:
        raw["node_evidence"][node_id] = [{"source_id": "s", "page": 1}]
    if links:
        raw["reference_links"] = {"tenant": raw["tenant"], "entries": [
            {"id": "contact", "label": "Contacto verificado", "url": "https://example.org/" + "a" * 1600,
             "status": "verified_reference", "review_after": "2026-10-11T06:30:00Z", "node_ids": selected}]}
        if links == "large":
            raw["reference_links"]["entries"] = [
                {"id": "contact" + str(index), "label": "Referencia " + "l" * 189,
                 "url": "https://example.org/" + str(index) + "a" * 1779,
                 "status": "verified_reference", "review_after": "2026-10-11T06:30:00Z", "node_ids": selected}
                for index in (1, 2)]
    bundle = content["normalize_bundle"](raw, 46, "tierra-del-fuego")
    state = {"bundle": bundle, "bundle_hash": content["digest"](bundle), "generation": 1, "visibility": "public"}
    state["revision"] = content["digest"]({key: state[key] for key in ("bundle_hash", "generation", "visibility")})
    tenant = SimpleNamespace(id=46, slug="tierra-del-fuego", is_active=True, municipio_id=460,
                             pyme_id=None, tipo="municipio")
    owner = SimpleNamespace(id=460, tenant_id=46)
    tenant_model, owner_model = object(), object()
    db = SimpleNamespace(session=SimpleNamespace(
        get=lambda cls, identifier: tenant if cls is tenant_model else owner,
        expire_all=lambda: None, no_autoflush=nullcontext()))
    selector_calls = []

    def read_state(observed_tenant, *, public):
        assert observed_tenant is tenant and public
        if state["visibility"] != "public":
            raise content["ContentError"]("knowledge_not_available", 404)
        return state

    def fake_answer(observed_tenant, command, *, public):
        assert observed_tenant is tenant and public and command["revision"] == state["revision"]
        node_id = command["node_id"]
        if "question" in command:
            def selector(*args):
                selector_calls.append(command["question"])
                return {"node_ids": selected}
            nodes = content["select_nodes"](bundle, command["question"], node_id, selector)
        else:
            nodes = [deepcopy(bundle["nodes"][node_id])]
        nodes = [content["materialize_node"](node, public=True) for node in nodes]
        return {"nodes": nodes, "text": "\n\n".join(node["text"] for node in nodes),
                "tenant": deepcopy(bundle["tenant"])}

    assistant = definitions("services/institutional_assistant.py", {
        "deepcopy": deepcopy, "db": db, "TenantProfile": tenant_model,
        "read_state": read_state, "ContentError": content["ContentError"], "answer": fake_answer,
        "_active_operational_context": lambda context: False,
        "audio_reading_capability": lambda: None, "_channel_source_label": lambda source: source["title"],
        "UI": {"error": "Synthetic error", "unknown": "Synthetic unknown", "sources": "Documentos y fuentes"},
        "_CHANNEL_UI_KEYS": (),
    }, {"maybe_handle_institutional_question", "_institutional_reply_code"})
    presentation = definitions("services/institutional_assistant_whatsapp.py", {
        "deepcopy": deepcopy, "re": re, "ContentError": content["ContentError"],
        "answer": fake_answer, "read_state": read_state,
    })
    cloud = definitions("services/meta_whatsapp_cloud.py", {
        "re": re, "_RECIPIENT": re.compile(r"^[1-9][0-9]{6,14}$"),
    }, {"MetaContractError", "text_payload", "list_payload"})
    pilot = definitions("services/tdf_meta_sandbox.py", {
        "db": db, "TenantProfile": tenant_model, "User": owner_model, "SimpleNamespace": SimpleNamespace,
        "re": re, "TENANT_ID": 46, "TENANT_SLUG": "tierra-del-fuego", "read_state": read_state,
        **{name: value for name, value in presentation.items() if callable(value)},
        "maybe_handle_institutional_question": assistant["maybe_handle_institutional_question"],
        "cloud": SimpleNamespace(**{name: cloud[name] for name in ("MetaContractError", "text_payload", "list_payload")}),
    }, {"PilotError", "_answer", "_reply_payload", "_compose_menu_answer", "_knowledge_revision_current"})
    return SimpleNamespace(pilot=pilot, presentation=presentation, assistant=assistant, bundle=bundle,
                           state=state, tenant=tenant, owner=owner, labels=labels, targets=destinations,
                           selected=selected, selector_calls=selector_calls)


def reply(case, text=None, context=None):
    event = SimpleNamespace(content_type="text", text=text or ("MENU" if len(case.selected) == 1 else "Tres temas sintéticos"))
    body, context, revision, buttons = case.pilot["_answer"](event, context or {})
    payload = case.pilot["_reply_payload"]("5491112345678", body, buttons, revision)
    return body, context, revision, buttons, payload


class TestMenuPresentation(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden"))
        self.subprocess = patch.object(subprocess, "run", side_effect=AssertionError("Subprocess forbidden"))
        self.network.start(); self.subprocess.start()
        self.addCleanup(self.network.stop); self.addCleanup(self.subprocess.stop)

    def test_eleven_multinode_actions_fallback_retains_every_unnumbered_label_and_target(self):
        case = fixture()
        body, context, revision, buttons, payload = reply(case)
        self.assertEqual(payload["type"], "text")
        self.assertEqual(len(buttons), 11)
        self.assertEqual(revision, case.state["revision"])
        self.assertEqual(context["institutional_whatsapp_sources"]["tenant"], {"id": 46, "slug": "tierra-del-fuego"})
        self.assertEqual(context["institutional_whatsapp_sources"]["node_ids"], case.selected)
        self.assertEqual(context["institutional_knowledge"]["reply_choices"], {})
        self.assertTrue(all("reply_code" not in button for button in buttons))
        self.assertEqual([button["action_id"] for button in buttons],
                         ["knowledge:" + revision[:16] + ":" + target for target in case.targets])
        for label in case.labels:
            self.assertIn("- " + label, body)
        self.assertNotIn("número", body)
        self.assertLessEqual(len(body), 4096)

    def test_long_single_node_retains_all_six_canonical_numeric_options(self):
        case = fixture(counts=(6,), text_length=3350)
        body, context, _, buttons, payload = reply(case)
        self.assertEqual(payload["type"], "text")
        self.assertEqual(len(buttons), 6)
        for index, label in enumerate(case.labels, 1):
            self.assertIn(str(index) + ". " + label, body)
        self.assertEqual(context["institutional_knowledge"]["reply_choices"],
                         {str(index): target for index, target in enumerate(case.targets, 1)})
        self.assertIn("número", body)
        self.assertLessEqual(len(body), 4096)

    def test_long_conflict_keeps_review_warning_sources_command_and_menu(self):
        case = fixture(counts=(6,), text_length=12000, review_status="conflict", links=True)
        body, _, _, buttons, _ = reply(case)
        self.assertEqual(len(buttons), 6)
        self.assertIn("Hay diferencias entre algunas fuentes que todavía deben revisarse.", body)
        self.assertIn("escribí FUENTES", body)
        self.assertIn("Información completa: https://www.chatboc.ar/t/tierra-del-fuego", body)
        for index, label in enumerate(case.labels, 1):
            self.assertIn(str(index) + ". " + label, body)
        self.assertLessEqual(len(body), 4096)
        self.assertNotIn("https://example.org/", body)

    def test_excessive_full_menu_closes_without_hidden_numeric_or_source_scope(self):
        case = fixture(counts=(30, 30, 30), label_length=160)
        body, context, _, buttons, payload = reply(case)
        self.assertEqual(payload["type"], "text")
        self.assertEqual(buttons, ())
        self.assertIn("MENÚ", body)
        self.assertIn("no", body.casefold())
        self.assertNotIn("institutional_whatsapp_sources", context)
        self.assertFalse(context.get("institutional_knowledge", {}).get("reply_choices"))
        self.assertLessEqual(len(body), 4096)

    def test_short_numbered_native_list_preserves_exact_codes_ids_and_labels(self):
        case = fixture(counts=(6,))
        body, _, revision, buttons, payload = reply(case)
        self.assertEqual(payload["type"], "interactive")
        rows = payload["interactive"]["action"]["sections"][0]["rows"]
        self.assertEqual([row["id"] for row in rows], [button["action_id"] for button in buttons])
        self.assertEqual([button["reply_code"] for button in buttons], [str(index) for index in range(1, 7)])
        self.assertTrue(all(row["id"].startswith("knowledge:" + revision[:16] + ":") for row in rows))
        self.assertLessEqual(len(body), 1024)

    def test_ten_choices_use_native_limit_and_eleven_stay_complete_text(self):
        for count in (10, 11):
            case = fixture(counts=(count,))
            body, _, _, buttons, payload = reply(case)
            self.assertEqual(len(buttons), count)
            self.assertEqual(payload["type"], "interactive" if count == 10 else "text")
            for index, label in enumerate(case.labels, 1):
                self.assertIn(str(index) + ". " + label, body)

    def test_long_pending_review_notice_and_complete_links_are_never_cut(self):
        case = fixture(counts=(6,), text_length=12000, review_status="unreviewed", links=True)
        body, _, _, _, _ = reply(case)
        self.assertIn("Parte de la documentación todavía tiene revisión pendiente.", body)
        self.assertIn("escribí FUENTES", body)
        self.assertLessEqual(len(body), 4096)
        self.assertNotIn("https://example.org/", body)
        short = fixture(counts=(1,), links=True)
        body, _, _, _, _ = reply(short)
        link = "https://example.org/" + "a" * 1600
        self.assertIn(link, body)
        self.assertNotIn("Información completa:", body)

    def test_explicit_target_and_numeric_selection_remain_canonical_without_new_selector(self):
        case = fixture()
        _, context, revision, _, _ = reply(case)
        result = reply(case, "knowledge:" + revision[:16] + ":" + case.targets[-1], context)
        self.assertIn("Destino elegido 11", result[0])
        self.assertEqual(len(case.selector_calls), 1)
        numbered = fixture(counts=(6,))
        _, context, _, _, _ = reply(numbered)
        result = reply(numbered, "6", context)
        self.assertIn("Destino elegido 6", result[0])
        self.assertEqual(numbered.selector_calls, [])

    def test_normal_present_answer_output_and_private_notice_are_unchanged(self):
        for visibility in ("public", "private"):
            for counts in ((1,), (1, 1), (1, 1, 1)):
                case = fixture(counts=counts, visibility=visibility, review_status="conflict")
                session = SimpleNamespace(tenant_id=46, context_data={})
                question = "MENU" if len(counts) == 1 else "Temas sintéticos"
                result = case.assistant["maybe_handle_institutional_question"](question, case.owner, session)
                body, _ = case.presentation["present_answer"](result, session.context_data,
                                                             tenant_id=46, tenant_slug="tierra-del-fuego")
                expected = "\n\n".join(["x" * 30] * len(counts)) + "\n\n"
                if visibility == "public":
                    expected += "Hay diferencias entre algunas fuentes que todavía deben revisarse.\n\n"
                expected += "Para consultar las referencias de esta orientación, escribí FUENTES."
                self.assertEqual(body, expected)

    def test_existing_revision_guard_rejects_replacement_and_retirement(self):
        case = fixture(counts=(1,))
        revision = case.state["revision"]
        self.assertTrue(case.pilot["_knowledge_revision_current"](revision))
        case.state["revision"] = "0" * 64
        self.assertFalse(case.pilot["_knowledge_revision_current"](revision))
        case.state["visibility"] = "private"
        with self.assertRaises(ValueError):
            case.pilot["_knowledge_revision_current"](revision)

    def test_expired_reference_links_disappear_with_same_revision_and_choices(self):
        case = fixture(counts=(1,), links=True)
        link = "https://example.org/" + "a" * 1600
        before = reply(case)
        self.assertIn(link, before[0])
        with patch.object(FixedDatetime, "now", return_value=datetime(2026, 10, 11, 6, 31, tzinfo=timezone.utc)):
            after = reply(case)
            sources = reply(case, "FUENTES", after[1])
        self.assertNotIn("https://example.org/", after[0])
        self.assertNotIn("https://example.org/", sources[0])
        self.assertEqual(before[2], after[2])
        self.assertEqual(before[3], after[3])
        self.assertEqual(sources[2], case.state["revision"])

    def test_excessive_menu_does_not_leave_sources_available_and_menu_recovers(self):
        case = fixture(counts=(30, 30, 30), label_length=160)
        _, context, _, _, _ = reply(case)
        unavailable = reply(case, "FUENTES", context)
        self.assertIsNone(unavailable[2])
        self.assertIn("no están disponibles", unavailable[0])
        restored = reply(case, "MENU", context)
        self.assertEqual(len(restored[3]), 3)
        self.assertEqual(restored[4]["type"], "interactive")

    def test_exact_4096_contract_boundary_is_kept_and_overflow_is_explicit(self):
        case = fixture(counts=(1,))
        compose = case.pilot["_compose_menu_answer"]
        parts = {"text": "", "links": [], "notices": ""}
        instructions = compose(parts, ())
        text = "ñ" * (4096 - len(instructions) - 2)
        exact = compose({**parts, "text": text}, ())
        self.assertEqual(len(exact), 4096)
        self.assertTrue(exact.startswith(text))
        overflow = compose({**parts, "text": text + "ñ"}, ())
        self.assertLessEqual(len(overflow), 4096)
        self.assertIn("Información completa:", overflow)
        payload = case.pilot["cloud"].text_payload(recipient="5491112345678", body=exact)
        self.assertEqual(payload["text"]["body"], exact)

    def test_two_large_valid_links_are_omitted_atomically_with_explicit_remission(self):
        case = fixture(counts=(1,), links="large")
        body, _, _, buttons, _ = reply(case)
        self.assertEqual(len(case.bundle["nodes"]["start"]["links"]), 2)
        self.assertEqual(len(buttons), 1)
        self.assertLessEqual(len(body), 4096)
        self.assertNotIn("https://example.org/", body)
        self.assertIn("Información completa:", body)
        self.assertIn("1. " + case.labels[0], body)

    def test_closed_menu_preserves_unrelated_context_and_does_not_mutate_input(self):
        case = fixture(counts=(30, 30, 30), label_length=160)
        original = {"custom_ui": {"preference": "large"}, "counter": 7}
        snapshot = deepcopy(original)
        _, context, _, buttons, _ = reply(case, context=original)
        self.assertEqual(buttons, ())
        self.assertEqual(original, snapshot)
        self.assertEqual(context["custom_ui"], snapshot["custom_ui"])
        self.assertEqual(context["counter"], 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
