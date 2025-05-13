import requests

def enviar_mensaje_whatsapp(numero, mensaje, api_key):
    url = f"https://api.callmebot.com/whatsapp.php?phone={numero}&text={mensaje}&apikey={api_key}"
    response = requests.get(url)
    return response.status_code == 200
