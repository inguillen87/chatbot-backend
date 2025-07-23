document.addEventListener('DOMContentLoaded', () => {
    const socket = io();

    const chatMessages = document.getElementById('chat-messages');
    const messageForm = document.getElementById('message-form');
    const messageInput = document.getElementById('message-input');

    const requestLocation = () => {
        if (navigator.geolocation) {
            navigator.geolocation.getCurrentPosition(
                (position) => {
                    const location = {
                        lat: position.coords.latitude,
                        lon: position.coords.longitude,
                    };
                    socket.emit('location', location);
                },
                (error) => {
                    console.error('Error getting location:', error);
                    // Notify the user that we couldn't get the location
                    const errorMessage = document.createElement('div');
                    errorMessage.innerText = 'No se pudo obtener tu ubicación.';
                    chatMessages.appendChild(errorMessage);
                }
            );
        } else {
            console.error('Geolocation is not supported by this browser.');
            // Notify the user that geolocation is not supported
            const errorMessage = document.createElement('div');
            errorMessage.innerText = 'La geolocalización no es compatible con este navegador.';
            chatMessages.appendChild(errorMessage);
        }
    };

    messageForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const message = messageInput.value;
        socket.emit('message', message);
        messageInput.value = '';
    });

    socket.on('message', (data) => {
        const messageElement = document.createElement('div');
        messageElement.innerText = data.respuesta;
        chatMessages.appendChild(messageElement);

        if (data.solicitar_ubicacion) {
            requestLocation();
        }
    });
});
