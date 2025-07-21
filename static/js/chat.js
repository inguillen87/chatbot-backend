document.addEventListener('DOMContentLoaded', () => {
    const socket = io();

    const chatMessages = document.getElementById('chat-messages');
    const messageForm = document.getElementById('message-form');
    const messageInput = document.getElementById('message-input');

    messageForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const message = messageInput.value;
        socket.emit('message', message);
        messageInput.value = '';
    });

    socket.on('message', (data) => {
        const messageElement = document.createElement('div');
        messageElement.innerText = data;
        chatMessages.appendChild(messageElement);
    });
});
