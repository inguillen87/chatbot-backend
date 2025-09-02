// This script assumes the following HTML structure exists:
// <div id="chat-messages"></div>
// <form id="message-form">
//   <input id="message-input" autocomplete="off" />
//   <button type="submit">Send</button>
//   <button type="button" id="attachment-button">📎</button>
//   <input type="file" id="attachment-input" style="display:none" />
// </form>
// <button id="record-button">Record</button>
// <button id="location-button">Share Location</button>

document.addEventListener('DOMContentLoaded', () => {
    // Connect to the server using Socket.IO
    const socket = io({
        query: { channel: 'web' }
    });
    socket.on('connect', () => {
        console.log('Socket connected, id:', socket.id);
        // Send an initial empty message to get the welcome message and menu
        socket.emit('message', { pregunta: '' });
    });
    socket.on('disconnect', () => {
        console.log('Socket disconnected');
    });
    socket.on('connect_error', (err) => {
        console.error('Socket connection error:', err);
    });

    // Get references to the necessary HTML elements
    const chatMessages = document.getElementById('chat-messages');
    const messageForm = document.getElementById('message-form');
    const messageInput = document.getElementById('message-input');
    const recordButton = document.getElementById('record-button');
    const locationButton = document.getElementById('location-button');
    const attachmentButton = document.getElementById('attachment-button');
    const attachmentInput = document.getElementById('attachment-input');

    let mediaRecorder;
    let audioChunks = [];
    let isRecording = false;

    // --- Attachment Logic ---
    if (attachmentButton && attachmentInput) {
        attachmentButton.addEventListener('click', () => attachmentInput.click());
        attachmentInput.addEventListener('change', () => {
            const file = attachmentInput.files[0];
            if (file) {
                sendAttachment(file);
                attachmentInput.value = '';
            }
        });
    }

    // --- Audio Recording Logic ---

    if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        console.log('getUserMedia supported.');

        recordButton.addEventListener('click', () => {
            if (isRecording) {
                // Stop recording
                mediaRecorder.stop();
                recordButton.textContent = 'Record';
                isRecording = false;
            } else {
                // Start recording
                navigator.mediaDevices.getUserMedia({ audio: true })
                    .then(stream => {
                        mediaRecorder = new MediaRecorder(stream);
                        mediaRecorder.start();
                        recordButton.textContent = 'Stop Recording';
                        isRecording = true;
                        audioChunks = []; // Clear previous chunks

                        mediaRecorder.addEventListener("dataavailable", event => {
                            audioChunks.push(event.data);
                        });

                        mediaRecorder.addEventListener("stop", () => {
                            const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
                            sendAudioMessage(audioBlob);
                        });
                    })
                    .catch(error => {
                        console.error('Error accessing microphone:', error);
                        alert('Could not access your microphone. Please check your browser permissions.');
                    });
            }
        });

    } else {
        console.error('getUserMedia not supported on your browser!');
        recordButton.disabled = true;
        recordButton.textContent = 'Recording Not Supported';
    }

    // --- Message Sending Logic ---

    // Send text message when the form is submitted
    messageForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const message = messageInput.value;
        if (message) {
            // Add the user's message to the chat window
            appendMessage('user', message);
            console.log('Sending message to server:', message);
            // Emit the message to the server
            socket.emit('message', { pregunta: message });
            messageInput.value = '';
        }
    });

    // Function to send audio message
    function sendAudioMessage(audioBlob) {
        const formData = new FormData();
        formData.append('audio_file', audioBlob, 'recording.webm');

        // Add a visual indicator that the audio is being sent
        appendMessage('user', '[Sending audio...]');

        // Use fetch to send the audio data to the /ask endpoint
        fetch('/ask/municipio', { // Assuming a default endpoint, adjust if necessary
            method: 'POST',
            body: formData,
            headers: {
                // 'Content-Type': 'multipart/form-data' is set automatically by the browser with FormData
                'X-Chat-Session-Id': getSessionId() // Important for session context
            }
        })
        .then(response => response.json())
        .then(data => {
            // The server will handle the audio and the response will come via Socket.IO
            console.log('Audio upload response:', data);
            console.log('Audio sent successfully, waiting for socket response.');
        })
        .catch(error => {
            console.error('Error sending audio:', error);
            appendMessage('system', 'Error sending audio.');
        });
    }

    // Function to handle file attachments
    function sendAttachment(file) {
        const formData = new FormData();
        formData.append('file', file);

        appendMessage('user', `[Adjunto: ${file.name}]`);

        fetch('/upload/chat_attachment', {
            method: 'POST',
            body: formData,
            headers: {
                'X-Chat-Session-Id': getSessionId()
            }
        })
        .then(response => response.json())
        .then(data => {
            if (data.ok && data.attachment_info) {
                socket.emit('message', {
                    pregunta: `[Archivo adjunto: ${file.name}]`,
                    attachment_info: data.attachment_info
                });
            } else {
                appendMessage('system', 'Error uploading attachment.');
            }
        })
        .catch(error => {
            console.error('Error uploading attachment:', error);
            appendMessage('system', 'Error uploading attachment.');
        });
    }

    // --- Location Logic ---

    locationButton.addEventListener('click', () => {
        if (navigator.geolocation) {
            navigator.geolocation.getCurrentPosition(
                (position) => {
                    const location = {
                        lat: position.coords.latitude,
                        lon: position.coords.longitude,
                    };
                    // Add a message indicating location is being sent
                    appendMessage('user', `[Sharing location: ${location.lat}, ${location.lon}]`);
                    // Send location data as part of a standard message payload
                    socket.emit('message', {
                        pregunta: '[Ubicación compartida por el usuario]',
                        location: location
                    });
                },
                (error) => {
                    console.error('Error getting location:', error);
                    appendMessage('system', 'Could not get your location.');
                }
            );
        } else {
            console.error('Geolocation is not supported by this browser.');
            appendMessage('system', 'Geolocation is not supported by this browser.');
        }
    });

    // --- Response Handling Logic ---

    // Listen for messages from the server
    socket.on('message', (data) => {
        console.log('Received message from server:', data);
        const messageText = data.respuesta || data.message_body || 'No response text.';

        // Add the bot's message to the chat window
        appendMessage('bot', messageText);

        // If the response contains an audio URL, play it
        if (data.audio_url) {
            playAudio(data.audio_url);
        }

        // If the response contains buttons, display them
        if (data.botones && data.botones.length > 0) {
            appendButtons(data.botones);
        }

        // If the server asks for more info, display a form
        if (data.pedir_info && Array.isArray(data.pedir_info)) {
            appendContactForm(data.pedir_info);
        }
    });

    // --- Helper Functions ---

    // Function to append a message to the chat window
    function appendMessage(sender, text) {
        const messageElement = document.createElement('div');
        messageElement.classList.add('message', `${sender}-message`);
        messageElement.innerText = text;
        chatMessages.appendChild(messageElement);
        chatMessages.scrollTop = chatMessages.scrollHeight; // Scroll to the bottom
    }

    // Function to play an audio URL
    function playAudio(url) {
        const container = document.createElement('div');
        container.classList.add('message', 'bot-message');

        const audioElement = document.createElement('audio');
        audioElement.src = url;
        audioElement.controls = true;
        audioElement.autoplay = true;

        audioElement.addEventListener('error', (error) => {
            console.error('Error playing audio:', error);
        });

        container.appendChild(audioElement);
        chatMessages.appendChild(container);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    // Function to display buttons
    function appendButtons(buttons) {
        const buttonContainer = document.createElement('div');
        buttonContainer.classList.add('button-container');
        buttons.forEach(buttonInfo => {
            const button = document.createElement('button');
            button.innerText = buttonInfo.texto;
            button.addEventListener('click', () => {
                const message = buttonInfo.action_id || buttonInfo.texto;
                appendMessage('user', message);
                socket.emit('message', { pregunta: message });
                // Remove buttons after one is clicked
                buttonContainer.remove();
            });
            buttonContainer.appendChild(button);
        });
        chatMessages.appendChild(buttonContainer);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    // Function to append a contact form
    function appendContactForm(fields) {
        const form = document.createElement('form');
        form.id = 'contact-form';

        fields.forEach(field => {
            const input = document.createElement('input');
            input.id = `contact-${field}`;
            input.name = field;
            input.placeholder = field.charAt(0).toUpperCase() + field.slice(1);
            input.required = true;
            form.appendChild(input);
        });

        const submitButton = document.createElement('button');
        submitButton.type = 'submit';
        submitButton.textContent = 'Enviar';
        form.appendChild(submitButton);

        chatMessages.appendChild(form);
        chatMessages.scrollTop = chatMessages.scrollHeight;

        form.addEventListener('submit', (e) => {
            e.preventDefault();
            const formData = new FormData(form);
            const data = Object.fromEntries(formData.entries());

            // Create a readable message from the data
            const message = Object.entries(data).map(([key, value]) => `${key}: ${value}`).join(', ');

            appendMessage('user', message);
            socket.emit('message', { pregunta: message });
            form.remove();
        });
    }

    // Function to get or generate a session ID (for stateful communication)
    function getSessionId() {
        let sessionId = sessionStorage.getItem('chat_session_id');
        if (!sessionId) {
            sessionId = `web-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
            sessionStorage.setItem('chat_session_id', sessionId);
        }
        return sessionId;
    }
});
