# Webchat real-time features

This backend exposes helper endpoints and Socket.IO events so the frontend can
attach media, share the user's location or start a video call with an agent.

## Attach images or voice notes
1. Capture a file from the browser (`<input type="file">`, camera or microphone).
2. Send a `POST` request to `/archivos/upload/chat_attachment` with a `FormData`
   field named `file`.
3. The response returns an `attachmentInfo` object containing `url`, `thumbUrl`
   and metadata. Include this object when calling `/ask` so the message is linked
to the chat session.

## Share location
- **REST**: send `POST /chat/location` with JSON `{ "lat": <float>, "lon": <float> }`.
- **Socket.IO**: emit the `location` event with the same payload to update the
  current session position.

## Start a video call
1. Join a room with `socket.emit('join', { room })`.
2. Notify agents with `socket.emit('call_agent', { room })`.
3. Exchange WebRTC data:
   - `socket.emit('webrtc_offer', { room, sdp })`
   - `socket.emit('webrtc_answer', { room, sdp })`
   - `socket.emit('webrtc_ice_candidate', { room, candidate })`
4. When the call ends, send `socket.emit('end_call', { room })`.

Agents listening on the same room will receive the mirrored events
(`incoming_call`, `webrtc_offer`, `webrtc_answer`, `webrtc_ice_candidate` and
`end_call`) to complete the signaling handshake.
