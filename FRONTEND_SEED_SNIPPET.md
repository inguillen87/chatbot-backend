# Frontend Integration: Survey Seed Tool

To add the "Seed 100 Participantes" feature to your Admin Frontend, use the following React component.
This component communicates with the new Backend API endpoint `/api/admin/encuestas/:id/seed-demo`.

## Backend API Requirement
The backend must have the `seed_demo_endpoint` enabled (already implemented in `routes/encuestas_admin.py`).

## React Component Snippet

You can drop this component into your survey details or list page.

```jsx
import React, { useState } from 'react';
import axios from 'axios'; // Or your preferred http client

/**
 * SeedButton Component
 *
 * Renders a button to populate a survey with mock data.
 *
 * @param {number|string} surveyId - The ID of the survey to seed.
 * @param {string} title - The title of the survey (for confirmation dialog).
 * @param {string} token - The auth token (JWT) for the API request.
 * @param {string} apiUrlBase - Base URL of your backend (e.g., "https://api.chatboc.ar").
 * @param {function} onSuccess - Callback function to refresh data after seeding.
 */
const SeedButton = ({ surveyId, title, token, apiUrlBase, onSuccess }) => {
  const [loading, setLoading] = useState(false);

  const handleSeed = async () => {
    if (!window.confirm(`¿Generar 100 respuestas de prueba para "${title}"?`)) {
      return;
    }

    setLoading(true);
    try {
      // The endpoint expected by the backend
      const endpoint = `${apiUrlBase}/api/admin/encuestas/${surveyId}/seed-demo`;

      const response = await axios.post(
        endpoint,
        {
          cantidad: 100, // Default requested amount
          // Optional: municipality_label: "Junín"
        },
        {
          headers: {
            'Authorization': token.startsWith('Bearer ') ? token : `Bearer ${token}`,
            'Content-Type': 'application/json'
          }
        }
      );

      if (response.data && response.data.creadas > 0) {
        alert(`✅ Éxito: Se generaron ${response.data.creadas} respuestas.`);
        if (onSuccess) onSuccess();
      } else {
        alert('⚠️ El proceso finalizó pero no se generaron respuestas (revisar logs).');
      }

    } catch (error) {
      console.error("Seed error:", error);
      const msg = error.response?.data?.error || error.message;
      alert(`❌ Error al generar datos: ${msg}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <button
      onClick={handleSeed}
      disabled={loading}
      className="btn btn-outline-success btn-sm d-flex align-items-center gap-2"
      title="Generar 100 respuestas de prueba"
    >
      {loading ? (
        <>
          <span className="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>
          <span>Procesando...</span>
        </>
      ) : (
        <>
          <span>🌱 Seed 100</span>
        </>
      )}
    </button>
  );
};

export default SeedButton;
```

## Integration Example

```jsx
// In your Survey List or Detail view
// ...
return (
  <div>
    <h1>{survey.titulo}</h1>

    <div className="actions">
       <SeedButton
         surveyId={survey.id}
         title={survey.titulo}
         token={auth.token} // Get from your auth context
         apiUrlBase={config.API_URL} // e.g. process.env.REACT_APP_API_URL
         onSuccess={() => reloadSurveyData()}
       />
    </div>
  </div>
);
```
