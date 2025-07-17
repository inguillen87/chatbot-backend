# services/actions/__init__.py
# This file makes the 'actions' module a package.

# ACTION_HANDLER_MAP defines the mapping from action names (determined by LLM/Orchestrator)
# to the fully qualified path of the handler class.
ACTION_HANDLER_MAP = {
    # Municipio Actions
    "iniciar_reclamo": "services.actions.municipio_actions.CrearReclamoActionHandler",
    "crear_reclamo": "services.actions.municipio_actions.CrearReclamoActionHandler",
    "consultar_estado_ticket": "services.actions.municipio_actions.ConsultarEstadoTicketActionHandler",
    "info_tramite": "services.actions.municipio_actions.ConsultarInfoTramiteActionHandler",
    "hacer_sugerencia": "services.actions.municipio_actions.HacerSugerenciaActionHandler",
    "ejecutar_herramienta": "services.actions.municipio_actions.EjecutarHerramientaActionHandler",
    "activar_panico": "services.actions.municipio_actions.ActivarPanicoActionHandler",
    "derivar_humano": "services.actions.municipio_actions.DerivarHumanoActionHandler",
    "procesar_adjunto_reclamo": "services.actions.municipio_actions.ProcesarAdjuntoReclamoActionHandler",
    "corregir_datos_reclamo": "services.actions.municipio_actions.CorregirDatosReclamoActionHandler",


    # PYME Actions
    "crear_pedido_pyme": "services.actions.pyme_order_actions.CrearPedidoAction", # Assuming existing
    "agregar_item_carrito": "services.actions.pyme_order_actions.AgregarItemCarritoAction", # Assuming existing
    "consultar_producto_pyme": "services.actions.pyme_order_actions.ConsultarProductoAction", # Assuming existing
    # "ver_carrito_pyme": "services.actions.pyme_actions.VerCarritoActionHandler", # Needs to be created or mapped
    # "finalizar_compra_pyme": "services.actions.pyme_actions.FinalizarCompraActionHandler", # Needs to be created or mapped

    # Common Actions
    "procesar_adjunto": "services.actions.common_actions.ProcesarAdjuntoAction", # Assuming existing
    "informar_usuario": "services.actions.common_actions.InformarUsuarioAction", # Assuming existing
    "registrar_usuario": "services.actions.general_actions.RegistrarUsuarioActionHandler",

    # Placeholder for actions that might not have a dedicated backend handler beyond LLM response
    "no_accion": "services.actions.general_actions.NoActionHandler",
    "small_talk": "services.actions.general_actions.SmallTalkActionHandler",
    "saludar": "services.actions.general_actions.SmallTalkActionHandler",
    "saludo": "services.actions.general_actions.SmallTalkActionHandler",

    # TODO: Add more mappings as handlers are implemented/confirmed
    # Municipio examples to confirm/create:
    # "consultar_info_tramite_municipio": "services.actions.municipio_claim_actions.ConsultarInfoTramiteAction",
    # "hacer_sugerencia_municipio": "services.actions.municipio_claim_actions.HacerSugerenciaAction",
    # "ejecutar_herramienta_municipio": "services.actions.municipio_tool_actions.EjecutarHerramientaAction", # Could be separate file
    # "activar_panico_municipio": "services.actions.municipio_alert_actions.ActivarPanicoAction", # Could be separate file
    # "corregir_datos_reclamo_municipio": "services.actions.municipio_claim_actions.CorregirDatosReclamoAction",
    # "procesar_adjunto_reclamo_municipio": "services.actions.municipio_claim_actions.ProcesarAdjuntoReclamoAction",

    # PYME examples to confirm/create:
    # "ver_carrito_pyme": "services.actions.pyme_order_actions.VerCarritoAction",
    # "modificar_carrito_pyme": "services.actions.pyme_order_actions.ModificarCarritoAction",
    # "finalizar_compra_pyme": "services.actions.pyme_order_actions.FinalizarCompraAction",
    # "consultar_ofertas_pyme": "services.actions.pyme_product_actions.ConsultarOfertasAction",
    # "solicitar_ubicacion_tienda_pyme": "services.actions.pyme_general_actions.SolicitarUbicacionTiendaAction",
    # "consultar_estado_pedido_pyme": "services.actions.pyme_order_actions.ConsultarEstadoPedidoAction",
    # "corregir_datos_pedido_pyme": "services.actions.pyme_order_actions.CorregirDatosPedidoAction",
    # "procesar_adjunto_pedido_pyme": "services.actions.pyme_order_actions.ProcesarAdjuntoPedidoAction",
}
