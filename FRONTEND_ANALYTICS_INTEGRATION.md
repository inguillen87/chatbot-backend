# Frontend Integration Guide: Advanced Analytics & AI Consultancy

This guide details the new endpoints available for the "Consultancy-Grade" Analytics Dashboard.

## 1. AI Consultant (Business Reports)

The AI Consultant analyzes sales, interactions, and sentiment to provide actionable business advice.

### A. Check for Latest Report (Cache)
**Endpoint:** `GET /api/analytics/report/latest`

*   **Objective:** Check if a fresh report already exists to avoid AI costs.
*   **Query Params:** `tenant_id`, `segment` (pyme/municipio).
*   **Response (200 OK):** JSON of the report content.
*   **Response (404 Not Found):** No valid cached report. UI should prompt user to "Generate New Report".

### B. Generate Fresh Report
**Endpoint:** `POST /api/analytics/report/generate`

*   **Description:** Generates a strategic analysis using GPT-4.
*   **Behavior:** The backend **caches** the result for 7 days. If a cached report exists (and `force=false`), it returns immediately.
*   **Request Payload:**
```json
{
  "tenant_id": 1,
  "segment": "pyme", // or "municipio"
  "from": "2023-10-01T00:00:00Z",
  "to": "2023-10-31T23:59:59Z",
  "force": false // Set true to bypass cache (Admin only recommended)
}
```

**Response:**
```json
{
  "summary": "Revenue increased by 15% driven by weekend promotions...",
  "opportunities": [
    "Increase stock of 'Taladro Percutor' due to high demand.",
    "Launch a rainy day promotion for 'Impermeabilizantes'."
  ],
  "threats": [
    "Customer response time has increased to 4 hours.",
    "Competitor X is dominating the 'Pinturas' category."
  ],
  "tone": "Professional",
  "_cached": true // Indicates if this came from DB or fresh AI call
}
```

## 2. Commerce Analytics (PyMEs)

**Endpoint:** `GET /api/analytics/sales`

*   **Description:** Dedicated sales metrics (Revenue, AOV, Conversion).
*   **Query Params:** `tenant_id`, `from`, `to`.

**Response:**
```json
{
  "revenue": 1500000.00,
  "average_ticket": 12500.50,
  "chat_conversion": 3.5, // % of chats leading to orders
  "total_orders": 120,
  "lead_source": [
    {"source": "whatsapp", "count": 800},
    {"source": "web_widget", "count": 200},
    {"source": "instagram", "count": 50}
  ],
  "sales_by_product": [
    {"name": "Taladro", "count": 15},
    {"name": "Martillo", "count": 10}
  ],
  "sales_by_hour": [
    {"hour": 9, "count": 5},
    {"hour": 10, "count": 12},
    // ...
    {"hour": 18, "count": 25} // Heatmap data
  ]
}
```

## 3. Historical Benchmarks

**Endpoint:** `GET /api/analytics/benchmarks`

*   **Description:** Compares current period vs previous period (e.g., Oct vs Sept).
*   **Query Params:** `tenant_id`, `from`, `to`.

**Response:**
```json
{
  "revenue": {
    "current": 15000.0,
    "previous": 12000.0,
    "growth_percentage": 25.0 // +25%
  },
  "interactions": {
    "current": 500,
    "previous": 450,
    "growth_percentage": 11.1
  }
}
```

## 4. User Funnel

**Endpoint:** `GET /api/analytics/funnel`

*   **Description:** Conversion funnel steps.
*   **Query Params:** `tenant_id`, `from`, `to`.

**Response:**
```json
{
  "steps": [
    {"name": "Opened Chat", "count": 1000},
    {"name": "Engaged", "count": 700},
    {"name": "Started Order", "count": 150},
    {"name": "Completed", "count": 120}
  ]
}
```

## 5. Citizen Surveys (Municipios)

### A. Summary
**Endpoint:** `GET /api/analytics/surveys/summary?tenant_id=X`
```json
{
  "active_survey": {"title": "Presupuesto 2025", "id": 5},
  "stats": {
    "total_votes": 1500,
    "participation_rate": 45.0,
    "results_by_option": [
      {"option": "Yes", "count": 900},
      {"option": "No", "count": 600}
    ]
  }
}
```

### B. Sentiment Analysis (AI)
**Endpoint:** `GET /api/analytics/surveys/sentiment?tenant_id=X`
*   **Note:** This endpoint is also **cached** (24h) to save costs.
```json
{
  "sentiment_score": 0.65, // -1.0 to 1.0
  "keywords": [
    {"word": "Seguridad", "count": 45},
    {"word": "Iluminación", "count": 30}
  ]
}
```

### C. Geo Voting
**Endpoint:** `GET /api/analytics/surveys/geo?tenant_id=X`
*   **Use Case:** Heatmap of where people are voting from.
```json
{
  "points": [
    {"lat": -34.60, "lng": -58.38, "weight": 1},
    // ...
  ]
}
```

## 6. Geospatial Map (Polygons)

**Endpoint:** `GET /api/geo/polygons?tenant_id=X`

*   **Description:** Returns dynamic GeoJSON polygons generated around the tenant's location.
*   **Frontend Usage:** Use with Leaflet (`L.geoJSON`) or Mapbox.
*   **Styling:** Use the `properties.density` (0-100) or `properties.fill` (hex color) to style the map layers (Chloropleth).

**Response (GeoJSON FeatureCollection):**
```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "properties": {
        "name": "Zona 0-0",
        "density": 85,
        "fill": "#FF0000"
      },
      "geometry": { "type": "Polygon", "coordinates": [...] }
    }
    // ...
  ]
}
```
