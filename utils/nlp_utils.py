import re

def encontrar_saludo(texto):
    """
    Busca un saludo en el texto.
    """
    if not texto:
        return False
    saludos = ["hola", "buenos dias", "buenas tardes", "buenas noches"]
    return any(saludo in texto.lower() for saludo in saludos)

def validar_telefono(telefono):
    """
    Valida un número de teléfono.
    """
    if not telefono:
        return False
    # Simple regex for phone numbers
    return re.match(r"^\+?[0-9]{10,15}$", telefono)
