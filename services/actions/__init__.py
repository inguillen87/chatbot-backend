from services.actions.pyme_actions import (
    MenuPrincipalHandler,
    CatalogoHandler,
    VerCarritoHandler,
    AgregarCarritoHandler,
    VaciarCarritoHandler,
    FinalizarCompraHandler,
    PromocionesHandler,
    InfoEnvioHandler,
)

# A mapping of action names to their corresponding handler classes.
ACTION_HANDLER_MAP = {
    # PYME Actions
    "pyme_menu_principal": MenuPrincipalHandler,
    "pyme_productos_stock": CatalogoHandler,
    "pyme_ver_carrito": VerCarritoHandler,
    "pyme_agregar_carrito": AgregarCarritoHandler,
    "pyme_vaciar_carrito": VaciarCarritoHandler,
    "pyme_finalizar_compra": FinalizarCompraHandler,
    "pyme_promociones": PromocionesHandler,
    "pyme_info_envio": InfoEnvioHandler,
    "pyme_hacer_pedido": AgregarCarritoHandler, # Alias for adding an item
}