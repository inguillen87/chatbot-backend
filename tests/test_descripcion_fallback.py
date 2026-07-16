import unittest
from unittest.mock import patch
from services.municipio_responder import accion_crear_reclamo_municipio


class AccionCrearReclamoDescripcionTest(unittest.TestCase):
    def test_descripcion_fallback_from_user_message(self):
        context = {"user_input_raw": "queria avisar que hay un agujero en la calle"}
        datos = {"categoria": "Arreglo de calle"}
        with patch("services.municipio_responder.CrearReclamoActionHandler") as handler_cls:
            exec_mock = handler_cls.return_value.execute
            exec_mock.return_value = {}
            accion_crear_reclamo_municipio(datos, context)
        handler_cls.assert_called_once_with(context=context)
        exec_mock.assert_called_once()
        sent_datos = exec_mock.call_args.args[0]
        self.assertEqual(sent_datos["descripcion"], context["user_input_raw"])

    def test_descripcion_fallback_with_full_details_message(self):
        mensaje = (
            "Hola soy Marcelo tengo un agujero en la calle que esta lleno de agua y obstruye el transito. "
            "Mi direccion es Sarmiento 133 esquina Av San Martin Junin centro."
        )
        context = {"user_input_raw": mensaje}
        datos = {"categoria": "Arreglo de calle"}
        with patch("services.municipio_responder.CrearReclamoActionHandler") as handler_cls:
            exec_mock = handler_cls.return_value.execute
            exec_mock.return_value = {}
            accion_crear_reclamo_municipio(datos, context)
        sent_datos = exec_mock.call_args.args[0]
        self.assertEqual(sent_datos["descripcion"], mensaje)


if __name__ == "__main__":
    unittest.main()
