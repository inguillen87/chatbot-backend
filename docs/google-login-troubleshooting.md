# Google login troubleshooting

If you see console errors such as:

```
[GSI_LOGGER]: The given origin is not allowed for the given client ID.
login:1 Access to fetch at 'https://api.chatboc.ar/google-login' from origin 'http://localhost:8080' has been blocked by CORS policy
```

it usually means your OAuth client ID is not authorised for the origin where the front-end is running.

Another common error is:

```
[GSI_LOGGER]: Parameter client_id is not set correctly.
```

This happens when the front-end does not set the `VITE_GOOGLE_CLIENT_ID` variable or its value does not match the backend's `GOOGLE_OAUTH_CLIENT_ID`.
Remember that Vite embeds these variables during build time. If you change
`VITE_GOOGLE_CLIENT_ID` later, rebuild the front-end so the new value is used.

To test locally:

1. In Google Cloud Console, edit the OAuth **Web application** that corresponds to `VITE_GOOGLE_CLIENT_ID` and add `http://localhost:8080` to its **Authorised JavaScript origins**.
2. Set the same client ID on the backend via the `GOOGLE_OAUTH_CLIENT_ID` environment variable.
3. Restart both backend and frontend so the new settings take effect.

The backend's default CORS configuration already whitelists `http://localhost:8080`, as seen in [`app.py`](../app.py).
