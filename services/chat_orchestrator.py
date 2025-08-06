# services/chat_orchestrator.py
import logging
import importlib
from typing import Dict, Any, type 
from services.actions.base_action_handler import BaseActionHandler
import importlib

logger = logging.getLogger(__name__)

class ChatOrchestrator:
    def __init__(self, db_session):
        self.db_session = db_session

    def _get_handler_class(self, action_name: str, target: str) -> Type[BaseActionHandler] | None:
        # Intenta obtener el handler específico para el target (ej. 'municipio' o 'pyme')
        module_name = f"services.actions.{target}_actions"
        class_name = f"{action_name.replace('_', ' ').title().replace(' ', '')}ActionHandler"

        try:
            module = importlib.import_module(module_name)
            handler_class = getattr(module, class_name, None)
            if handler_class:
                logger.info(f"Handler '{class_name}' encontrado en el módulo específico '{module_name}'.")
                return handler_class
        except ImportError:
            logger.warning(f"No se encontró el módulo de acciones específicas: '{module_name}'.")

        # Si no se encuentra un handler específico, busca en los handlers generales/comunes
        module_name = "services.actions.general_actions"
        try:
            module = importlib.import_module(module_name)
            handler_class = getattr(module, class_name, None)
            if handler_class:
                logger.info(f"Handler '{class_name}' encontrado en el módulo general '{module_name}'.")
                return handler_class
        except ImportError:
            logger.error(f"No se pudo importar el módulo de acciones generales: '{module_name}'.")

        logger.warning(f"No se encontró una clase de handler '{class_name}' en los módulos buscados.")
        return None

    def route_action(self, llm_response: Dict[str, Any]) -> Dict[str, Any]:
        action_name = llm_response.get("accion_backend")
        datos_estructura = llm_response.get("datos_estructura", {})
        target = datos_estructura.get("target", "general") # 'municipio', 'pyme', etc.

        if not action_name:
            logger.warning("No se especificó 'accion_backend' en la respuesta del LLM.")
            # Directly return the error response without needing to instantiate a handler
            return {
                "success": False,
                "message_to_user": "La IA no especificó una acción.",
                "fuente": "error_no_action"
            }

        # Acciones genéricas que no necesitan un handler y se devuelven directamente
        if action_name in ["responder_directamente", "saludar", "no_accion", "small_talk", "respuesta_generica"]:
            logger.info(f"Acción genérica '{action_name}' no requiere handler. Devolviendo respuesta del LLM.")
            return {
                "success": True,
                "message_to_user": llm_response.get("respuesta_usuario", "Entendido."),
                "data": {"action_performed": action_name},
                "executed_action_handler": "GenericResponseHandler",
                "botones": llm_response.get("botones", [])
            }

        handler_class = self._get_handler_class(action_name, target)

        if handler_class:
            try:
                handler_instance = handler_class(self.db_session)
                handler_data = {**datos_estructura, "respuesta_usuario_original_llm": llm_response.get("respuesta_usuario")}
                logger.info(f"Executing action '{action_name}' with handler '{handler_class.__name__}' and data: {handler_data}")
                return handler_instance.execute(handler_data)
            except Exception as e:
                logger.error(f"Error al ejecutar el handler para la acción '{action_name}': {e}", exc_info=True)
                return {
                    "success": False,
                    "message_to_user": f"Error interno al procesar la acción: {action_name}.",
                    "fuente": "error_handler_execution"
                }
        else:
            logger.error(f"No se encontró un handler para la acción: '{action_name}' con target '{target}'.")
            return {
                "success": False,
                "message_to_user": f"Acción '{action_name}' desconocida o no implementada.",
                "fuente": "error_handler_not_found"
            }

if __name__ == '__main__': # pragma: no cover
    # Example Usage (requires ACTION_HANDLER_MAP and handlers to be defined correctly)

    # Mock global context that would be built in responder_pyme or responder_municipio
    mock_global_context = {
        "user_obj": {"id": 1, "nombre_empresa": "Municipio Test", "municipio_id": "test_muni"}, # Simulating owner_user
        "viewer_user_obj": {"id": 101, "name": "Vecino Prueba"}, # Simulating viewer_user
        "cliente_id": 101,
        "anon_id": None,
        "channel": "web",
        "chat_db_context_data": { # This is the live dict from chat_db_context.context_data
            "carritos_pymes": {}, # Example for pyme cart
            "contexto_municipio_v2": {}, # Example for municipio state
            "contexto_pyme_v2": {} # Example for pyme state
        },
        "nombre_usuario_contexto": "Vecino Contextual", # Fallback names from context
        "telefono_usuario_contexto": "+5492619998877",
        "email_usuario_contexto": "vecino.context@example.com",
        # ... other global context items ...
    }

    orchestrator = ChatOrchestrator(global_context=mock_global_context)

    # 1. Simulate LLM output for creating a reclamo
    llm_output_reclamo = {
        "accion_backend": "crear_reclamo", # This should match a key in ACTION_HANDLER_MAP
        "datos_estructura": {
            "categoria": "Alumbrado Público",
            "descripcion": "Luz quemada en poste",
            "ubicacion": "Calle Falsa 123, Ciudad Gótica",
            "nombre_usuario_detectado": "Bruno Díaz",
            "telefono_detectado": "+15551234567"
        },
        "respuesta_usuario": "Entendido, voy a registrar tu reclamo por la luminaria."
    }
    print(f"\n--- Test 1: Crear Reclamo ---")
    result_reclamo = orchestrator.execute_action(llm_output_reclamo)
    print(f"Resultado de Crear Reclamo: {result_reclamo}")

    # 2. Simulate LLM output for consulting product (PYME)
    llm_output_producto_pyme = {
        "accion_backend": "consultar_producto_pyme",
        "datos_estructura": {
            "nombre_producto_mencionado": "zapatillas deportivas",
            "target": "pyme"
        },
        "respuesta_usuario": "Buscando zapatillas deportivas..."
    }
    # Update global context if needed for PYME scenario
    mock_global_context_pyme = mock_global_context.copy()
    mock_global_context_pyme["user_obj"] = {"id": 2, "nombre_empresa": "Tienda Deportiva Acme", "rubro": {"nombre": "Deportes"}}
    orchestrator_pyme = ChatOrchestrator(global_context=mock_global_context_pyme)

    print(f"\n--- Test 2: Consultar Producto PYME ---")
    result_producto_pyme = orchestrator_pyme.execute_action(llm_output_producto_pyme)
    print(f"Resultado de Consultar Producto PYME: {result_producto_pyme}")

    # 3. Simulate No Action
    llm_output_no_action = {
        "accion_backend": "no_accion",
        "respuesta_usuario": "Entendido, no hay problema.",
        "datos_estructura": {}
    }
    print(f"\n--- Test 3: No Acción ---")
    result_no_action = orchestrator.execute_action(llm_output_no_action)
    print(f"Resultado de No Acción: {result_no_action}")

    # 4. Simulate Small Talk
    llm_output_small_talk = {
        "accion_backend": "small_talk",
        "respuesta_usuario": "¡Yo muy bien! ¿Y tú?",
        "datos_estructura": {}
    }
    print(f"\n--- Test 4: Small Talk ---")
    result_small_talk = orchestrator.execute_action(llm_output_small_talk)
    print(f"Resultado de Small Talk: {result_small_talk}")

    # 5. Simulate unknown action
    llm_output_unknown = {
        "accion_backend": "accion_desconocida_xxx",
        "respuesta_usuario": "Intentando acción desconocida...",
        "datos_estructura": {}
    }
    print(f"\n--- Test 5: Acción Desconocida ---")
    result_unknown = orchestrator.execute_action(llm_output_unknown)
    print(f"Resultado de Acción Desconocida: {result_unknown}")

    # 6. Simulate action requiring more info (e.g., CrearReclamoAction returns pedir_info)
    llm_output_reclamo_incompleto = {
        "accion_backend": "crear_reclamo",
        "datos_estructura": { # Missing 'ubicacion'
            "categoria": "Arbol Caido",
            "descripcion": "Rama grande bloquea la vereda",
        },
        "respuesta_usuario": "Necesito más datos para tu reclamo de árbol caído."
    }
    print(f"\n--- Test 6: Crear Reclamo Incompleto (simulando que handler pide info) ---")
    # Note: The mock CrearReclamoAction in municipio_claim_actions.py might need to be
    # adjusted to actually return a "pedir_info" field for this test to be meaningful.
    # For now, we just test the orchestrator's path.
    result_reclamo_incompleto = orchestrator.execute_action(llm_output_reclamo_incompleto)
    print(f"Resultado de Reclamo Incompleto: {result_reclamo_incompleto}")
    if result_reclamo_incompleto.get("pedir_info"):
        print(f"  Orchestrator correctly propagated 'pedir_info': {result_reclamo_incompleto['pedir_info']}")
    elif llm_output_reclamo_incompleto.get("pedir_info"):
         print(f"  LLM output already had 'pedir_info': {llm_output_reclamo_incompleto['pedir_info']}")


    # Example for AgregarItemCarritoAction
    llm_output_agregar_carrito = {
        "accion_backend": "agregar_item_carrito",
        "datos_estructura": {
            "producto_nombre": "Vino Malbec Reserva", # Or "producto_sku": "VMR001"
            "cantidad": 2,
            "target": "pyme"
        },
        "respuesta_usuario": "Agregando 2 Vino Malbec Reserva al carrito..."
    }
    print(f"\n--- Test 7: Agregar Item Carrito PYME ---")
    # Assuming pyme_id 2, cliente_id 101
    mock_global_context_pyme["cliente_id"] = 101
    mock_global_context_pyme["chat_db_context_data"]['carritos_pymes'] = {2: []} # Ensure cart for pyme 2 exists
    orchestrator_pyme_cart = ChatOrchestrator(global_context=mock_global_context_pyme)
    result_agregar_carrito = orchestrator_pyme_cart.execute_action(llm_output_agregar_carrito)
    print(f"Resultado de Agregar Item Carrito: {json.dumps(result_agregar_carrito, indent=2, ensure_ascii=False)}")
    # print(f"Estado del carrito PYME 2 después: {mock_global_context_pyme['chat_db_context_data']['carritos_pymes'].get(2)}")

    # Example for CrearPedidoAction (assuming items are in cart)
    # For this to work, the cart for pyme_id 2 should have items.
    # Let's assume AgregarItemCarritoAction added them.
    llm_output_crear_pedido = {
        "accion_backend": "crear_pedido_pyme",
        "datos_estructura": {
            "nombre_usuario_detectado": "Consumidor Final",
            "telefono_detectado": "+5492617776655",
            "email_detectado": "cf@example.com",
            "ubicacion": "Calle de Entrega 456", # For direccion_entrega
            "target": "pyme"
        },
        "respuesta_usuario": "Finalizando tu pedido..."
    }
    print(f"\n--- Test 8: Crear Pedido PYME ---")
    # orchestrator_pyme_cart should still have the context with the updated cart
    result_crear_pedido = orchestrator_pyme_cart.execute_action(llm_output_crear_pedido)
    print(f"Resultado de Crear Pedido: {json.dumps(result_crear_pedido, indent=2, ensure_ascii=False)}")
    # print(f"Estado del carrito PYME 2 después del pedido: {mock_global_context_pyme['chat_db_context_data']['carritos_pymes'].get(2)}") # Should be empty if cleared
