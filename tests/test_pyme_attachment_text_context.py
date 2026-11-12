"""Real PYME consumer/media intake on disposable ORM; providers are synthetic."""
from copy import deepcopy
from unittest.mock import patch
import unittest

from models import PedidoConversacional, TenantProfile, TenantTicket, User, db
from services import pyme_multimodal as media_flow
from services.pymes import responder_pyme
from tests.test_pyme_location_text_context import PymeLocationTextContextTests


class PymeAttachmentTextContextTests(unittest.TestCase):
    setUp = PymeLocationTextContextTests.setUp
    _remove_database = PymeLocationTextContextTests._remove_database
    turn = PymeLocationTextContextTests.turn
    state = PymeLocationTextContextTests.state
    TEXT = "Necesito cotizar cuatro cajas de tornillos para mañana."

    @staticmethod
    def attachment(kind, **extra):
        return {"url": "https://fixture.invalid/nota." + ("png" if kind == "image" else "pdf"),
                "mime_type": "image/png" if kind == "image" else "application/pdf",
                "name": "nota." + ("png" if kind == "image" else "pdf"), **extra}

    def media_turn(self, kind, text="", actor=None, anon_id=None, **extra):
        info = self.attachment(kind, **extra)
        with patch.object(media_flow, "analyse_image_for_products", return_value=[]) as analyse:
            response = self.turn({"pregunta": text, "uploaded_file_info": info}, actor=actor, anon_id=anon_id)
        self.llm.assert_not_called()
        if kind == "image":
            analyse.assert_called_once()
        return response, info

    @staticmethod
    def source(response):
        return response["data"]["assisted_request"]["source"]

    def test_body_with_unread_attachment_is_preserved_without_repeating_request(self):
        for kind in ("image", "pdf"):
            with self.subTest(kind=kind):
                response, _ = self.media_turn(kind, self.TEXT)
                source = self.source(response)
                self.assertIn(self.TEXT, source.get("text_preview", ""))
                self.assertEqual(source.get("declared_text", {}).get("message"), self.TEXT)
                self.assertNotIn("qué necesitás", response["message_body"])
                self.assertNotIn("contarme qué necesitás", response["message_body"])

    def test_caption_and_body_remain_separate_without_mutating_upload_metadata(self):
        caption = "Entrega en recepción; la foto es de referencia."
        for kind in ("image", "pdf"):
            with self.subTest(kind=kind):
                info = self.attachment(kind, caption=caption)
                original = deepcopy(info)
                with patch.object(media_flow, "analyse_image_for_products", return_value=[]):
                    response = self.turn({"pregunta": self.TEXT, "uploaded_file_info": info})
                source = self.source(response)
                self.assertEqual(source["declared_text"], {"message": self.TEXT, "caption": caption})
                self.assertEqual(info, original)
                self.assertIn(self.TEXT, source["text_preview"])
                self.assertIn(caption, source["text_preview"])

    def test_identical_body_and_caption_are_previewed_once_but_keep_both_origins(self):
        response, _ = self.media_turn("image", self.TEXT, caption=self.TEXT)
        source = self.source(response)
        self.assertEqual(source["declared_text"], {"message": self.TEXT, "caption": self.TEXT})
        self.assertEqual(source["text_preview"].count(self.TEXT), 1)

    def test_caption_only_does_not_ask_to_repeat_the_declared_request(self):
        for kind in ("image", "pdf"):
            with self.subTest(kind=kind):
                response, _ = self.media_turn(kind, caption=self.TEXT)
                self.assertEqual(self.source(response)["declared_text"], {"caption": self.TEXT})
                self.assertNotIn("qué necesitás", response["message_body"])

    def test_attachment_only_keeps_existing_request_for_missing_detail(self):
        for kind in ("image", "pdf"):
            with self.subTest(kind=kind):
                response, _ = self.media_turn(kind)
                self.assertNotIn("declared_text", self.source(response))
                self.assertIn("qué necesitás", response["message_body"])

    def test_declared_text_is_bounded_and_nonstring_values_are_not_serialized(self):
        for text, caption, expected in (("x" * 2600, "y" * 2600, {"message": "x" * 2000, "caption": "y" * 2000}),
                                        ("", {"private": "not text"}, {})):
            with self.subTest(kind="bounds" if text else "invalid_type"):
                response, _ = self.media_turn("image", text, caption=caption)
                source = self.source(response)
                self.assertEqual(source.get("declared_text", {}), expected)
                self.assertLessEqual(len(source.get("text_preview", "")), 500)
                self.assertNotIn("private", source.get("text_preview", ""))

    def test_extraction_failure_preserves_text_without_claiming_reading_success(self):
        info = self.attachment("image")
        with patch.object(media_flow, "analizar_imagen_con_fallback", side_effect=RuntimeError("synthetic OCR failure")):
            response = self.turn({"pregunta": self.TEXT, "uploaded_file_info": info})
        self.assertEqual(self.source(response)["declared_text"]["message"], self.TEXT)
        self.assertEqual(response["fuente"], "pyme_imagen_revision_manual")
        self.assertNotIn("Leí tu pedido", response["message_body"])
        self.assertNotIn("synthetic OCR failure", response["message_body"])

    def test_parsed_media_does_not_replace_declared_text_or_parse_it_as_media(self):
        for kind in ("image", "pdf"):
            with self.subTest(kind=kind):
                self.session.context_data = {}
                db.session.commit()
                info = self.attachment(kind)
                if kind == "pdf":
                    info["analysis"] = {"items": [{"nombre": "Malbec de prueba", "cantidad": 1}],
                                        "texto_extraido": "Lista del documento: Malbec"}
                with patch.object(media_flow, "analyse_image_for_products", return_value=[{"nombre": "Malbec de prueba", "cantidad": 1}]):
                    response = self.turn({"pregunta": self.TEXT, "uploaded_file_info": info})
                source = self.source(response)
                self.assertEqual(source["declared_text"]["message"], self.TEXT)
                self.assertEqual(self.state()["cart"]["items"][0]["qty"], 1)
                self.assertEqual(response["data"]["assisted_request"]["match_summary"]["detected"], 1)

    def test_wrong_session_scope_cannot_start_media_intake_or_ocr(self):
        other_owner = User(name="Otra tienda", email="other-attachment-owner@example.test",
                           password_hash="test-only", rol="admin", tipo_chat="pyme")
        db.session.add(other_owner)
        db.session.flush()
        other_tenant = TenantProfile(slug="other-attachment-test", nombre="Otra tienda", tipo="pyme",
                                     pyme_id=other_owner.id, is_active=True)
        db.session.add(other_tenant)
        db.session.commit()
        for invalid in ("anon", "tenant", "authenticated_actor"):
            for kind in ("image", "pdf"):
                with self.subTest(invalid=invalid, kind=kind):
                    self.session.tenant_id = self.tenant.id
                    self.session.user_id = self.owner.id
                    self.session.anon_id = "anon-location-text"
                    self.session.context_data = {}
                    kwargs = {}
                    if invalid == "anon":
                        kwargs["anon_id"] = "another-person"
                    elif invalid == "tenant":
                        self.session.tenant_id = other_tenant.id
                    else:
                        self.session.anon_id = None
                        kwargs["actor"] = self.viewer
                    db.session.commit()
                    before = (PedidoConversacional.query.count(), TenantTicket.query.count())
                    with patch.object(media_flow, "analyse_image_for_products", side_effect=AssertionError("OCR forbidden before scope")) as analyse:
                        response = self.turn({"pregunta": self.TEXT, "uploaded_file_info": self.attachment(kind)}, **kwargs)
                    self.assertFalse(response["success"])
                    self.assertEqual(response["fuente"], "pyme_attachment_scope_invalid")
                    self.assertEqual((PedidoConversacional.query.count(), TenantTicket.query.count()), before)
                    analyse.assert_not_called()

    def test_saved_whatsapp_contact_keeps_canonical_anon_and_intake_tenant(self):
        response, _ = self.media_turn("image", self.TEXT, actor=self.viewer)
        pedido = db.session.get(PedidoConversacional, response["data"]["pedido_id"])
        self.assertEqual(pedido.tenant_id, self.tenant.id)
        self.assertEqual(pedido.anon_id, self.session.anon_id)
        self.assertEqual(pedido.metadata_payload["source"]["declared_text"]["message"], self.TEXT)

    def test_authenticated_web_actor_keeps_intake_in_current_tenant(self):
        self.session.user_id = self.viewer.id
        self.session.anon_id = None
        db.session.commit()
        response, _ = self.media_turn("pdf", self.TEXT, actor=self.viewer)
        pedido = db.session.get(PedidoConversacional, response["data"]["pedido_id"])
        self.assertEqual(pedido.tenant_id, self.tenant.id)
        self.assertEqual(pedido.user_id, self.viewer.id)
        self.assertIsNone(pedido.anon_id)
        self.assertEqual(pedido.metadata_payload["source"]["declared_text"]["message"], self.TEXT)

    def test_existing_direct_handler_calls_without_new_argument_remain_compatible(self):
        for kind in ("image", "pdf"):
            with self.subTest(kind=kind):
                state = media_flow.PymeSessionState({}, self.owner.id)
                handler = media_flow.handle_image_payload if kind == "image" else media_flow.handle_pdf_payload
                with patch.object(media_flow, "analyse_image_for_products", return_value=[]):
                    result = handler(state, self.attachment(kind), [], owner_user_id=self.owner.id)
                self.assertNotIn("declared_text", result.data["assisted_request"]["source"])
                self.assertIn("qué necesitás", result.message_body)

    def test_direct_handler_without_tenant_does_not_claim_saved_request(self):
        for kind in ("image", "pdf"):
            with self.subTest(kind=kind):
                state = media_flow.PymeSessionState({}, self.owner.id)
                handler = media_flow.handle_image_payload if kind == "image" else media_flow.handle_pdf_payload
                before = PedidoConversacional.query.count()
                with patch.object(media_flow, "analyse_image_for_products", return_value=[]):
                    result = handler(state, self.attachment(kind), [], owner_user_id=self.owner.id,
                                     current_message_text=self.TEXT)
                self.assertEqual(result.data["assisted_request"]["persistence_skipped"], "missing_owner_or_tenant")
                self.assertIn("no pude registrar", result.message_body)
                self.assertNotIn("quedó registrado", result.message_body)
                self.assertNotIn("podés agregar", result.message_body)
                self.assertEqual(PedidoConversacional.query.count(), before)

    def test_failure_before_commit_returns_no_success_ack_and_does_not_retry(self):
        for kind in ("image", "pdf"):
            with self.subTest(kind=kind):
                before = PedidoConversacional.query.count()
                with patch.object(media_flow, "analyse_image_for_products", return_value=[]), \
                     patch.object(media_flow, "_materialize_assisted_intake_ticket", side_effect=RuntimeError("synthetic transaction failure")) as materialize:
                    with self.assertRaisesRegex(RuntimeError, "synthetic transaction failure"):
                        self.turn({"pregunta": self.TEXT, "uploaded_file_info": self.attachment(kind)})
                materialize.assert_called_once()
                db.session.rollback()
                self.assertEqual(PedidoConversacional.query.count(), before)

    def test_postcommit_ack_loss_keeps_one_saved_request_without_automatic_retry(self):
        for kind in ("image", "pdf"):
            with self.subTest(kind=kind):
                before = PedidoConversacional.query.count()
                with patch.object(media_flow, "analyse_image_for_products", return_value=[]), \
                     patch.object(media_flow, "track_marketplace_event", side_effect=RuntimeError("synthetic postcommit ACK loss")) as track:
                    with self.assertRaisesRegex(RuntimeError, "synthetic postcommit ACK loss"):
                        self.turn({"pregunta": self.TEXT, "uploaded_file_info": self.attachment(kind)})
                track.assert_called_once()
                db.session.expire_all()
                self.assertEqual(PedidoConversacional.query.count(), before + 1)
                pedido = PedidoConversacional.query.order_by(PedidoConversacional.id.desc()).first()
                self.assertEqual(pedido.metadata_payload["source"]["declared_text"]["message"], self.TEXT)
