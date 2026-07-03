
import unittest
from unittest.mock import patch, MagicMock
from app import create_app, db
from config import TestConfig
from models import AnalyticsEventV2, CatalogoItem, PedidoConversacional, TenantProfile, TenantTicket, User
from services.pymes import responder_pyme, CONTEXTO_PYME
from services.pyme_multimodal import PymeSessionState, handle_image_payload, handle_pdf_payload


def _assert_public_copy_without_internal_jargon(*parts):
    visible_copy = "\n".join(part for part in parts if part)
    for forbidden in ("CRM", "IA", "tenant admin", "auditoria", "operativo", "operativa"):
        assert forbidden not in visible_copy


class PymeMultimodalTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.pymes.llamar_llm_con_fallback')
    @patch('services.pymes.handle_image_payload') # Mock handling of image
    def test_responder_pyme_with_image(self, mock_handle_image, mock_llm):
        # We need handle_image_payload to NOT return a flow result so that responder_pyme proceeds to LLM
        # OR we check that it calls handle_image_payload correctly.
        # But actually, responder_pyme calls handle_image_payload and returns EARLY if it returns something.

        # Scenario 1: Image handled by logic (e.g. Visual Search) -> Early Return
        mock_flow_result = MagicMock()
        mock_flow_result.message_body = "Encontré estos productos similares"
        mock_flow_result.source = "visual_search"
        mock_flow_result.data = {}
        mock_flow_result.options_list = []
        mock_flow_result.message_type = "text"
        mock_flow_result.audio_url = None
        mock_flow_result.audio_text = None
        mock_flow_result.delayed_payload = None
        mock_flow_result.delay_seconds = 20

        mock_handle_image.return_value = mock_flow_result

        owner_user = MagicMock()
        owner_user.id = 1
        owner_user.rubro.slug = "general"
        owner_user.tenant_profile_pyme = None
        chat_db_context = MagicMock()
        chat_db_context.context_data = {CONTEXTO_PYME: {}}

        uploaded_info = {
            "url": "http://example.com/image.jpg",
            "mime_type": "image/jpeg"
        }

        with patch('services.pymes.load_catalog', return_value=[]):
             response = responder_pyme(
                pregunta_original="",
                owner_user=owner_user,
                rubro_obj=owner_user.rubro,
                chat_db_context=chat_db_context,
                uploaded_file_info=uploaded_info
            )

        self.assertIn("Encontré estos productos", response['message_body'])
        mock_handle_image.assert_called_once()
        mock_llm.assert_not_called() # Should return early

    @patch('services.pyme_multimodal.buscar_catalogo_qdrant', return_value=[])
    @patch('services.pyme_multimodal.analyse_image_for_products')
    def test_image_payload_creates_assisted_intake_request_for_crm(self, mock_analyse, _mock_qdrant):
        owner = User(name="Ferreteria Demo", email="owner@example.com", password_hash="x", rol="admin")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(slug="ferreteria-demo", nombre="Ferreteria Demo", tipo="pyme", pyme_id=owner.id)
        db.session.add(tenant)
        db.session.flush()
        db.session.add(
            CatalogoItem(
                user_id=owner.id,
                tenant_id=tenant.id,
                nombre="Chapa galvanizada",
                sku="CH-001",
                precio="12000",
                modalidad="venta",
                disponible=True,
            )
        )
        db.session.commit()

        mock_analyse.return_value = [
            {"nombre": "Chapa galvanizada", "cantidad": 2},
            {"nombre": "Clavos 2 pulgadas", "cantidad": 1},
        ]

        state = PymeSessionState({}, owner.id)
        result = handle_image_payload(
            state,
            {
                "url": "https://cdn.example.com/pedido.jpg",
                "name": "pedido.jpg",
                "mime_type": "image/jpeg",
                "id": "media-1",
            },
            [
                {
                    "id": 10,
                    "catalogo_item_id": 10,
                    "sku": "CH-001",
                    "nombre": "Chapa galvanizada",
                    "precio": "12000",
                    "moneda": "ARS",
                    "presentacion": "unidad",
                }
            ],
            request_id="req-123",
            owner_user_id=owner.id,
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            channel="whatsapp",
            context={
                "nombre_cliente": "Marcelo",
                "telefono_cliente": "+5492613168608",
                "email_cliente": "marcelo@example.com",
            },
            anon_id="+5492613168608",
        )

        self.assertIsNotNone(result)
        assisted_request = result.data["assisted_request"]
        self.assertEqual(assisted_request["contract_version"], "marketplace.assisted_request.v1")
        self.assertEqual(assisted_request["source"]["channel"], "whatsapp")
        self.assertEqual(assisted_request["contact"]["phone"], "+5492613168608")
        intake = assisted_request["intake_experience"]
        _assert_public_copy_without_internal_jargon(
            assisted_request["customer_message"],
            intake["title"],
            intake["summary"],
            *(f"{step['label']} {step['description']}" for step in intake["pipeline"]),
            intake["crm_handoff"]["label"],
            *(f"{step['label']} {step['description']}" for step in assisted_request["customer_next_steps"]),
            *(f"{action['label']} {action['description']}" for action in assisted_request["next_actions"]),
        )
        self.assertEqual(
            assisted_request["match_summary"],
            {
                "matched": 1,
                "unmatched": 1,
                "detected": 2,
                "needs_operator_review": True,
            },
        )
        self.assertEqual(assisted_request["unmatched_items"], ["Clavos 2 pulgadas"])
        crm_order_draft = assisted_request["crm_order_draft"]
        self.assertEqual(crm_order_draft["contract_version"], "marketplace.crm_order_draft.v1")
        self.assertEqual(crm_order_draft["pedido_id"], assisted_request["pedido_id"])
        self.assertEqual(crm_order_draft["reference"], f"pedido:{assisted_request['pedido_id']}")
        self.assertEqual(crm_order_draft["source"]["channel"], "whatsapp")
        self.assertEqual(crm_order_draft["contact_state"], "available")
        self.assertEqual(crm_order_draft["recommended_next_step"], "resolver_items_y_confirmar")
        self.assertEqual(
            [line["status"] for line in crm_order_draft["lines"]],
            ["catalog_matched", "needs_catalog_resolution"],
        )
        self.assertEqual(crm_order_draft["lines"][0]["catalog_item_id"], 10)
        self.assertEqual(crm_order_draft["lines"][1]["source_name"], "Clavos 2 pulgadas")
        self.assertEqual(result.data["pedido_id"], assisted_request["pedido_id"])
        self.assertEqual(assisted_request["linked_record"]["kind"], "tenant_ticket")
        self.assertEqual(assisted_request["ticket_type"], "tenant_ticket")
        self.assertEqual(assisted_request["public_follow_up"]["tracking"]["code"], f"pc-{assisted_request['pedido_id']}")
        self.assertIn("/tracking/order/pc-", assisted_request["public_follow_up"]["tracking"]["path"])

        pedido = db.session.get(PedidoConversacional, assisted_request["pedido_id"])
        self.assertIsNotNone(pedido)
        self.assertEqual(pedido.tipo, "nota_de_pedido")
        self.assertEqual(pedido.estado, "nuevo")
        self.assertEqual(pedido.origen, "whatsapp")
        self.assertEqual(pedido.metadata_payload["source"]["channel"], "whatsapp")
        self.assertEqual(pedido.metadata_payload["contract_version"], "marketplace.assisted_request.v1")
        self.assertEqual(pedido.metadata_payload["operator_pack"]["reference"], f"pedido:{pedido.id}")
        self.assertEqual(pedido.metadata_payload["crm_order_draft"]["reference"], f"pedido:{pedido.id}")
        self.assertEqual(pedido.metadata_payload["linked_record"]["id"], assisted_request["intake_ticket_id"])
        self.assertEqual(pedido.metadata_payload["public_follow_up"]["tracking"]["code"], f"pc-{pedido.id}")
        self.assertEqual(pedido.metadata_payload["match_summary"]["unmatched"], 1)
        self.assertEqual(pedido.items[0]["items_detectados"][0]["catalog_match"]["sku"], "CH-001")
        self.assertEqual(pedido.items[0]["no_encontrados"][0]["nombre"], "Clavos 2 pulgadas")
        self.assertEqual(pedido.items[0]["crm_order_draft"]["contract_version"], "marketplace.crm_order_draft.v1")

        intake_ticket = db.session.get(TenantTicket, assisted_request["intake_ticket_id"])
        self.assertIsNotNone(intake_ticket)
        self.assertEqual(intake_ticket.categoria, "marketplace_assisted_order")
        self.assertEqual(intake_ticket.origen, "whatsapp")
        self.assertEqual(intake_ticket.datos_extra["source"], "pyme_multimodal")
        self.assertEqual(intake_ticket.datos_extra["pedido_conversacional_id"], pedido.id)
        self.assertEqual(intake_ticket.datos_extra["public_follow_up"]["tracking"]["code"], f"pc-{pedido.id}")
        self.assertEqual(intake_ticket.datos_extra["attachments"][0]["url"], "https://cdn.example.com/pedido.jpg")

        event = AnalyticsEventV2.query.filter_by(
            tenant_id=tenant.id,
            event_name="assisted_multimodal_intake_submitted",
            entity_ref=f"pedido:{pedido.id}",
        ).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.metadata_payload["source"], "pyme_multimodal")
        self.assertEqual(event.metadata_payload["linked_record_type"], "tenant_ticket")
        self.assertEqual(event.metadata_payload["linked_record_id"], assisted_request["intake_ticket_id"])

    @patch('services.pyme_multimodal.analyse_image_for_products', return_value=[])
    def test_image_payload_persists_manual_review_when_unreadable(self, _mock_analyse):
        owner = User(name="Ferreteria Demo", email="owner2@example.com", password_hash="x", rol="admin")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(slug="ferreteria-demo", nombre="Ferreteria Demo", tipo="pyme", pyme_id=owner.id)
        db.session.add(tenant)
        db.session.commit()

        state = PymeSessionState({}, owner.id)
        result = handle_image_payload(
            state,
            {
                "url": "https://cdn.example.com/manuscrito-borroso.jpg",
                "name": "manuscrito-borroso.jpg",
                "mime_type": "image/jpeg",
                "caption": "pedido escrito a mano",
                "id": "media-unreadable",
            },
            [],
            request_id="req-unreadable",
            owner_user_id=owner.id,
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            channel="whatsapp",
            context={
                "nombre_cliente": "Marcelo",
                "telefono_cliente": "+5492613168608",
            },
            anon_id="+5492613168608",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.source, "pyme_imagen_revision_manual")
        assisted_request = result.data["assisted_request"]
        self.assertEqual(assisted_request["source"]["input_type"], "image_manual_review")
        self.assertEqual(assisted_request["source"]["channel"], "whatsapp")
        self.assertEqual(assisted_request["match_summary"]["detected"], 0)
        self.assertEqual(assisted_request["match_summary"]["matched"], 0)
        self.assertEqual(assisted_request["match_summary"]["unmatched"], 1)
        self.assertTrue(assisted_request["match_summary"]["needs_operator_review"])
        self.assertEqual(assisted_request["crm_order_draft"]["recommended_next_step"], "resolver_items_y_confirmar")

        pedido = db.session.get(PedidoConversacional, assisted_request["pedido_id"])
        self.assertIsNotNone(pedido)
        self.assertEqual(pedido.origen, "whatsapp")
        self.assertEqual(pedido.metadata_payload["source"]["input_type"], "image_manual_review")
        self.assertTrue(pedido.metadata_payload["match_summary"]["needs_operator_review"])
        self.assertEqual(pedido.metadata_payload["linked_record"]["kind"], "tenant_ticket")
        self.assertEqual(pedido.metadata_payload["public_follow_up"]["tracking"]["code"], f"pc-{pedido.id}")
        self.assertEqual(pedido.items[0]["extraction_error"], "imagen_sin_lectura")
        self.assertEqual(pedido.items[0]["no_encontrados"][0]["status"], "needs_operator_review")
        intake_ticket = db.session.get(TenantTicket, assisted_request["intake_ticket_id"])
        self.assertIsNotNone(intake_ticket)
        self.assertEqual(intake_ticket.datos_extra["attachments"][0]["url"], "https://cdn.example.com/manuscrito-borroso.jpg")

    def test_pdf_payload_persists_manual_review_without_catalog_match(self):
        owner = User(name="Mayorista Demo", email="owner3@example.com", password_hash="x", rol="admin")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(slug="mayorista-demo", nombre="Mayorista Demo", tipo="pyme", pyme_id=owner.id)
        db.session.add(tenant)
        db.session.commit()

        state = PymeSessionState({}, owner.id)
        result = handle_pdf_payload(
            state,
            {
                "name": "pedido-ferretero.pdf",
                "mime_type": "application/pdf",
                "texto_extraido": "necesito clavos, chapas y tornillos para entregar manana",
                "id": None,
            },
            [],
            request_id="req-pdf-unmatched",
            owner_user_id=owner.id,
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            channel="chat_widget",
            context={
                "nombre_cliente": "Cliente Mostrador",
                "email_cliente": "cliente@example.com",
            },
            anon_id="anon-market",
        )

        self.assertEqual(result.source, "pyme_pdf_revision_manual")
        assisted_request = result.data["assisted_request"]
        self.assertEqual(assisted_request["source"]["input_type"], "pdf_manual_review")
        self.assertEqual(assisted_request["source"]["channel"], "chat_widget")
        self.assertEqual(assisted_request["match_summary"]["detected"], 0)
        self.assertEqual(assisted_request["match_summary"]["unmatched"], 1)
        self.assertTrue(assisted_request["match_summary"]["needs_operator_review"])
        self.assertEqual(assisted_request["crm_order_draft"]["recommended_next_step"], "resolver_items_y_confirmar")

        pedido = db.session.get(PedidoConversacional, assisted_request["pedido_id"])
        self.assertIsNotNone(pedido)
        self.assertEqual(pedido.origen, "chat_widget")
        self.assertEqual(pedido.metadata_payload["source"]["input_type"], "pdf_manual_review")
        self.assertEqual(pedido.metadata_payload["linked_record"]["kind"], "tenant_ticket")
        self.assertEqual(pedido.metadata_payload["public_follow_up"]["tracking"]["code"], f"pc-{pedido.id}")
        self.assertEqual(pedido.items[0]["extraction_error"], "pdf_sin_match")
        self.assertIn("clavos", pedido.metadata_payload["source"]["text_preview"])
        intake_ticket = db.session.get(TenantTicket, assisted_request["intake_ticket_id"])
        self.assertIsNotNone(intake_ticket)
        self.assertEqual(intake_ticket.origen, "chat_widget")
        self.assertEqual(intake_ticket.datos_extra["pedido_conversacional_id"], pedido.id)

    @patch('services.pymes.llamar_llm_con_fallback')
    def test_responder_pyme_with_attachment_no_text(self, mock_llm):
        # Mock LLM response
        mock_llm.return_value = ({
            "accion_backend": "responder_directamente",
            "message_body": "Recibido el archivo."
        }, None)

        owner_user = MagicMock()
        owner_user.id = 1
        owner_user.rubro.nombre = "general"
        owner_user.rubro.slug = "general"
        # Mock tenant_profile_pyme to prevent NoneType error in slug resolution
        tenant_profile = MagicMock()
        tenant_profile.slug = "test_tenant"
        owner_user.tenant_profile_pyme = tenant_profile

        chat_db_context = MagicMock()
        chat_db_context.context_data = {CONTEXTO_PYME: {}}

        # Simulate attachment info without mime_type to trigger the fallback logic
        uploaded_info = {
            "url": "http://example.com/file.xyz",
            "name": "file.xyz",
            # No mime_type or unknown
        }

        response = responder_pyme(
            pregunta_original="",
            owner_user=owner_user,
            rubro_obj=owner_user.rubro,
            chat_db_context=chat_db_context,
            uploaded_file_info=uploaded_info
        )

        mock_llm.assert_called_once()
        self.assertNotIn("Sugerencia", response["message_body"])

        # Check what was sent to LLM
        args, _ = mock_llm.call_args
        # args[1] is message_usuario
        # args[0] is app
        if len(args) > 1:
            mensaje_usuario = args[1]
            self.assertIn("adjunt", mensaje_usuario)
        else:
            # Depending on how it was called (kwargs vs args)
            call_kwargs = mock_llm.call_args.kwargs
            mensaje_usuario = call_kwargs.get('mensaje_usuario')
            self.assertIn("adjunt", mensaje_usuario)

if __name__ == '__main__':
    unittest.main()
