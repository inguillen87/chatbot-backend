# Google login troubleshooting

If you see console errors such as:

```
[GSI_LOGGER]: The given origin is not allowed for the given client ID.
login:1 Access to fetch at 'https://api.chatboc.ar/auth/google-login' from origin 'http://localhost:8080' has been blocked by CORS policy
```

it usually means your OAuth client ID is not authorised for the origin where the front-end is running.

To test locally:

1. In Google Cloud Console, edit the OAuth **Web application** that corresponds to `VITE_GOOGLE_CLIENT_ID` and add `http://localhost:8080` to its **Authorised JavaScript origins**.
2. Set the same client ID on the backend via the `GOOGLE_OAUTH_CLIENT_ID` environment variable.
3. Restart both backend and frontend so the new settings take effect.

The backend's default CORS configuration already whitelists `http://localhost:8080`, as seen in [`app.py`](../app.py).
