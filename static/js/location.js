function solicitarUbicacion() {
    if (navigator.geolocation) {
        navigator.geolocation.getCurrentPosition(enviarUbicacion, manejarError);
    } else {
        alert("La geolocalización no es soportada por este navegador.");
    }
}

function enviarUbicacion(posicion) {
    const latitud = posicion.coords.latitude;
    const longitud = posicion.coords.longitude;

    // Enviar la ubicación al backend
    fetch('/chat/location', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ latitud, longitud }),
    })
    .then(response => response.json())
    .then(data => {
        console.log('Ubicación enviada:', data);
    })
    .catch((error) => {
        console.error('Error al enviar la ubicación:', error);
    });
}

function manejarError(error) {
    switch(error.code) {
        case error.PERMISSION_DENIED:
            alert("El usuario denegó la solicitud de geolocalización.");
            break;
        case error.POSITION_UNAVAILABLE:
            alert("La información de la ubicación no está disponible.");
            break;
        case error.TIMEOUT:
            alert("La solicitud para obtener la ubicación del usuario ha caducado.");
            break;
        case error.UNKNOWN_ERROR:
            alert("Un error desconocido ha ocurrido.");
            break;
    }
}
