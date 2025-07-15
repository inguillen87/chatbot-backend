# This file makes the 'services' directory a Python package.
# services/actions/__init__.py
# This file makes the 'actions' module a package.

# ACTION_HANDLER_MAP defines the mapping from action names (determined by LLM/Orchestrator)
# to the fully qualified path of the handler class.
ACTION_HANDLER_MAP = {
    # Municipio Actions
    "iniciar_reclamo": "services.actions.municipio_claim_actions.CrearReclamoAction",
    "crear_reclamo": "services.actions.municipio_claim_actions.CrearReclamoAction", # Assuming existing class name
    "consultar_estado_reclamo": "services.actions.municipio_claim_actions.ConsultarEstadoReclamoAction", # Assuming
    "consultar_reclamo": "services.actions.municipio_claim_actions.ConsultarEstadoReclamoAction",
    # "consultar_info_tramite_municipio": "services.actions.municipio_actions.ConsultarInfoTramiteActionHandler", # Needs to be created or mapped
    # "hacer_sugerencia_municipio": "services.actions.municipio_actions.HacerSugerenciaActionHandler", # Needs to be created or mapped
    # "ejecutar_herramienta_municipio": "services.actions.municipio_actions.EjecutarHerramientaActionHandler", # Needs to be created or mapped
    # "activar_panico_municipio": "services.actions.municipio_actions.ActivarPanicoActionHandler", # Needs to be created or mapped

    # PYME Actions
    "crear_pedido_pyme": "services.actions.pyme_order_actions.CrearPedidoAction", # Assuming existing
    "agregar_item_carrito": "services.actions.pyme_order_actions.AgregarItemCarritoAction", # Assuming existing
    "consultar_producto_pyme": "services.actions.pyme_order_actions.ConsultarProductoAction", # Assuming existing
    # "ver_carrito_pyme": "services.actions.pyme_actions.VerCarritoActionHandler", # Needs to be created or mapped
    # "finalizar_compra_pyme": "services.actions.pyme_actions.FinalizarCompraActionHandler", # Needs to be created or mapped

    # Common Actions
    "derivar_humano": "services.actions.common_actions.DerivarHumanoAction", # Assuming existing
    "procesar_adjunto": "services.actions.common_actions.ProcesarAdjuntoAction", # Assuming existing
    "informar_usuario": "services.actions.common_actions.InformarUsuarioAction", # Assuming existing

    # Placeholder for actions that might not have a dedicated backend handler beyond LLM response
    "no_accion": None, # Or a generic NoOpHandler if specific logging/tracking is needed
    "small_talk": None, # Usually handled by LLM response directly

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
