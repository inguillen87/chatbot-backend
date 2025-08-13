document.addEventListener('DOMContentLoaded', () => {
    const chatMessages = document.getElementById('chat-messages');
    const messageForm = document.getElementById('message-form');
    const messageInput = document.getElementById('message-input');
    // Keep the audio and location buttons for future use, but focus on text chat.
    const recordButton = document.getElementById('record-button');
    const locationButton = document.getElementById('location-button');

    // --- Core Message Sending Logic ---

    async function sendMessage(payload) {
        // Add user's message to the chat window immediately
        // The payload for a text message is { "pregunta": "user's text" }
        if (payload.pregunta) {
            appendMessage('user', payload.pregunta);
        }

        try {
            const response = await fetch('/ask/municipio', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Chat-Session-Id': getSessionId()
                },
                body: JSON.stringify(payload)
            });

            if (!response.ok) {
                // Handle server errors (like 500)
                const errorData = await response.json().catch(() => ({ message: 'Error fetching response from server.' }));
                throw new Error(errorData.message || `HTTP error! status: ${response.status}`);
            }

            const data = await response.json();
            handleBotResponse(data);

        } catch (error) {
            console.error('Error sending message:', error);
            appendMessage('bot', '⚠️ No se pudo generar una respuesta.');
        }
    }

    // --- Event Listeners ---

    // Handle text message submission
    messageForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const messageText = messageInput.value.trim();
        if (messageText) {
            sendMessage({ pregunta: messageText });
            messageInput.value = '';
        }
    });

    // --- Response Handling ---

    function handleBotResponse(data) {
        console.log('Received data from server:', data);

        // Use the response text from the server
        const messageText = data.message_body || 'No response text.';
        appendMessage('bot', messageText);

        // If the response contains an audio URL, play it
        if (data.audio_url) {
            playAudio(data.audio_url);
        }

        // If the response contains buttons or options, display them
        const options = data.options_list || data.botones;
        if (options && options.length > 0) {
            appendButtons(options);
        }
    }

    // --- UI Helper Functions ---

    function appendMessage(sender, text) {
        const messageElement = document.createElement('div');
        messageElement.classList.add('message', `${sender}-message`);
        // Use innerHTML to render formatted text from the bot
        messageElement.innerHTML = text.replace(/\n/g, '<br>');
        chatMessages.appendChild(messageElement);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    function playAudio(url) {
        const audioElement = new Audio(url);
        audioElement.play().catch(e => console.error('Error playing audio:', e));
    }

    function appendButtons(buttons) {
        const buttonContainer = document.createElement('div');
        buttonContainer.classList.add('button-container');

        buttons.forEach(buttonInfo => {
            const button = document.createElement('button');
            button.innerText = buttonInfo.texto || buttonInfo.label; // Support both formats

            button.addEventListener('click', () => {
                // Use the action_id if available, otherwise use the text
                const action = buttonInfo.action_id || buttonInfo.key || buttonInfo.texto || buttonInfo.label;
                sendMessage({ pregunta: action });

                // Remove buttons after one is clicked to prevent multiple clicks
                buttonContainer.remove();
            });
            buttonContainer.appendChild(button);
        });

        chatMessages.appendChild(buttonContainer);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    // --- Session Management ---

    function getSessionId() {
        let sessionId = sessionStorage.getItem('chat_session_id');
        if (!sessionId) {
            sessionId = `web-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
            sessionStorage.setItem('chat_session_id', sessionId);
        }
        return sessionId;
    }

    // Optional: Send an initial message to get the welcome menu
    // sendMessage({ pregunta: 'mostrar_menu' });
});
