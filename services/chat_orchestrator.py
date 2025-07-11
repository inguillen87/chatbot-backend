# services/chat_orchestrator.py
import logging
import importlib
from typing import Dict, Any
from services.actions import ACTION_HANDLER_MAP # Import the map

logger = logging.getLogger(__name__)

class ChatOrchestrator:
    def __init__(self, global_context: Dict[str, Any]):
        """
        Initializes the ChatOrchestrator with a global context.
        This context is passed to the instantiated action handlers.
        It should contain things like 'user_obj', 'viewer_user_obj', 'cliente_id',
        'anon_id', 'channel', 'chat_db_context_data', etc.
        """
        self.global_context = global_context

    def _get_handler_class(self, action_name: str):
        """
        Dynamically imports and returns the handler class for the given action name.
        """
        handler_path_str = ACTION_HANDLER_MAP.get(action_name)
        if not handler_path_str:
            logger.warning(f"No handler found for action: {action_name}")
            return None

        try:
            module_path, class_name = handler_path_str.rsplit('.', 1)
            module = importlib.import_module(module_path)
            handler_class = getattr(module, class_name)
            return handler_class
        except (ImportError, AttributeError) as e:
            logger.error(f"Error importing handler for action '{action_name}' with path '{handler_path_str}': {e}", exc_info=True)
            return None

    def execute_action(self, llm_output: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes the appropriate action based on the LLM output.

        :param llm_output: The structured JSON output from the main Gemini LLM call.
                           Expected to have "accion_backend" and "datos_estructura".
        :return: A dictionary containing the result from the executed action handler.
                 If no action is taken or an error occurs, it returns a failure indication.
        """
        action_name = llm_output.get("accion_backend")
        action_data = llm_output.get("datos_estructura", {}) # This is 'datos_accion' for the handler

        # Pass the original LLM's respuesta_usuario to action_data for handlers like NoAction or SmallTalk
        if "respuesta_usuario" in llm_output:
            action_data["respuesta_usuario_original_llm"] = llm_output["respuesta_usuario"]


        if not action_name or action_name in ["no_accion", "small_talk"]:
            # For "no_accion" or "small_talk", the LLM's "respuesta_usuario" is usually sufficient.
            # We can optionally call a specific handler if we need to log these or do minor backend tasks.
            handler_class = self._get_handler_class(action_name or "no_accion") # Default to no_accion if name is None
            if handler_class:
                # Pass the global_context to the handler instance
                handler_instance = handler_class(self.global_context)
                # The action_data for these might be minimal or just the LLM's original response for context
                action_result = handler_instance.execute(action_data)
                action_result["executed_action_handler"] = handler_class.__name__
                return action_result
            else:
                # If no specific handler for no_accion/small_talk, just return LLM's response
                return {
                    "success": True, # Considered success as LLM handled it
                    "message_to_user": llm_output.get("respuesta_usuario", "Entendido."),
                    "data": {"action_performed": action_name or "none"},
                    "executed_action_handler": None
                }

        handler_class = self._get_handler_class(action_name)
        if not handler_class:
            logger.error(f"Could not find or import handler for action: {action_name}")
            return {
                "success": False,
                "message_to_user": "Hubo un problema al procesar tu solicitud (acción desconocida).",
                "error_details": f"Handler for action '{action_name}' not found."
            }

        try:
            # Pass the global_context to the handler instance
            handler_instance = handler_class(self.global_context)
            logger.info(f"Executing action '{action_name}' with handler '{handler_class.__name__}' and data: {action_data}")
            action_result = handler_instance.execute(action_data)
            action_result["executed_action_handler"] = handler_class.__name__ # Add which handler ran

            # If the action handler indicates a need to ask for more info, propagate that
            if action_result.get("pedir_info"):
                llm_output["pedir_info"] = action_result["pedir_info"]

            return action_result

        except Exception as e:
            logger.error(f"Error executing action '{action_name}' with handler '{handler_class.__name__}': {e}", exc_info=True)
            return {
                "success": False,
                "message_to_user": "Ocurrió un error interno al realizar la acción solicitada.",
                "error_details": str(e),
                "executed_action_handler": handler_class.__name__
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
