function solicitarUbicacion() {
    if (navigator.geolocation) {
        navigator.geolocation.getCurrentPosition(enviarUbicacion, manejarError);
    } else {
        console.error("La geolocalización no es soportada por este navegador.");
    }
}

function enviarUbicacion(posicion) {
    const latitud = posicion.coords.latitude;
    const longitud = posicion.coords.longitude;
    const token = localStorage.getItem('token');

    if (!token) {
        console.error('No se encontró token de autenticación.');
        return;
    }

    fetch('/chat/location', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${token}`
        },
        body: JSON.stringify({ latitud, longitud }),
    })
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            console.error('Error al enviar la ubicación:', data.error);
        } else {
            console.log('Ubicación enviada:', data);
        }
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
