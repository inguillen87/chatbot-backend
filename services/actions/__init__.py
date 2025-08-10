# services/actions/__init__.py

# Este diccionario mapea el valor de 'accion_backend' del LLM a la clase de handler correspondiente.
# La clave es el string de la acción, y el valor es el path completo para importar la clase.

ACTION_HANDLER_MAP = {
    # Acciones Comunes
    "derivar_humano": "services.actions.common_actions.DerivarHumanoAction",
    "procesar_adjunto": "services.actions.common_actions.ProcesarAdjuntoAction",
    "informar_usuario": "services.actions.common_actions.InformarUsuarioAction",
    "descargar_archivo": "services.actions.common_actions.DescargarArchivoActionHandler",

    # Acciones Generales (sin lógica de negocio específica)
    "no_accion": "services.actions.general_actions.NoActionHandler",
    "small_talk": "services.actions.general_actions.SmallTalkActionHandler",
    "error": "services.actions.general_actions.ErrorActionHandler",
    "registrar_usuario": "services.actions.general_actions.RegistrarUsuarioActionHandler",
    "menu_principal": "services.actions.general_actions.MenuPrincipalActionHandler",
    "finalizar_tramite": "services.actions.general_actions.FinalizarTramiteActionHandler",

    # Acciones de Municipio
    "crear_reclamo": "services.actions.municipio_actions.CrearReclamoActionHandler",
    "iniciar_reclamo": "services.actions.municipio_actions.CrearReclamoActionHandler", # Usa el mismo handler, que pedirá datos
    "consultar_estado_ticket": "services.actions.municipio_actions.ConsultarEstadoTicketActionHandler",
    "info_tramite": "services.actions.municipio_actions.ConsultarInfoTramiteActionHandler",
    "hacer_sugerencia": "services.actions.municipio_actions.HacerSugerenciaActionHandler",
    "ejecutar_herramienta": "services.actions.municipio_actions.EjecutarHerramientaActionHandler",
    "activar_panico": "services.actions.municipio_actions.ActivarPanicoActionHandler",
    "corregir_datos": "services.actions.municipio_actions.CorregirDatosReclamoActionHandler",
    "consultar_puntos_de_interes": "services.actions.information_actions.ConsultarPuntosDeInteresActionHandler",

    # Acciones de PYME (Generales)
    "consultar_producto_pyme": "services.actions.pyme_actions.ConsultarProductoActionHandler",
    "consultar_ofertas_pyme": "services.actions.pyme_actions.ConsultarOfertasActionHandler",
    "solicitar_ubicacion_tienda": "services.actions.pyme_actions.SolicitarUbicacionTiendaActionHandler",
    "consultar_estado_pedido": "services.actions.pyme_actions.ConsultarEstadoPedidoActionHandler",
    "hacer_sugerencia_pyme": "services.actions.pyme_actions.HacerSugerenciaActionHandler",
    "info_tramite_pyme": "services.actions.pyme_actions.ConsultarInfoPymeActionHandler",

    # Acciones de Pedidos/Carrito de PYME
    "crear_pedido_pyme": "services.actions.pyme_order_actions.CrearPedidoAction",
    "agregar_item_carrito": "services.actions.pyme_order_actions.AgregarItemCarritoAction",
    "ver_carrito": "services.actions.pyme_order_actions.VerCarritoAction",
    "modificar_carrito": "services.actions.pyme_order_actions.ModificarCarritoAction",
    "finalizar_compra": "services.actions.pyme_order_actions.FinalizarCompraAction",
    "finalizar_pedido_pyme": "services.actions.pyme_order_actions.CrearPedidoAction",
    "procesar_adjunto_pedido": "services.actions.pyme_order_actions.ProcesarAdjuntoPedidoAction",
    "corregir_datos_pedido": "services.actions.pyme_actions.CorregirDatosPedidoActionHandler",

    # Saludos (pueden tener lógica específica por_entidad si es necesario)
    "saludar": "services.actions.pyme_actions.SaludoHandler", # O un SaludoHandler genérico
    "fallback": "services.pymes.FallbackHandler",

    # Handlers for PYME interactive menu
    "pyme_productos_stock": "services.actions.pyme_actions.CatalogoHandler",
    "pyme_promociones": "services.actions.pyme_actions.OfertasHandler",
    "pyme_estado_pedido": "services.actions.pyme_actions.ConsultarEstadoPedidoActionHandler",
    "pyme_hacer_pedido": "services.actions.pyme_order_actions.CrearPedidoAction",
    "pyme_hablar_agente": "services.actions.pyme_actions.HumanHandler",
    "pyme_otras_consultas": "services.actions.pyme_actions.OtrasConsultasHandler",
    "pyme_factura": "services.actions.pyme_actions.FacturaHandler",
}
