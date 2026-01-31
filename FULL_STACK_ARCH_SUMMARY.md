# Jules Full Stack Architecture Handover

This document summarizes the current state of the backend architecture and provides a clear guide for frontend implementation, ensuring a cohesive "SaaS-grade" system.

## 1. Backend Architecture

### Source of Truth
*   **Postgres:** Primary database for all structured data (Users, Tenants, Orders, Tickets, Catalog Items, Surveys).
*   **Qdrant:** Semantic index *only*. Syncs from Postgres. Used for vector search (RAG) and recommendations.
    *   `catalog_items`: Products and services.
    *   `knowledge_docs`: Municipal documents, ordinances, FAQs.
*   **File Storage:** Blob storage (S3/GCS/Cloudinary) for actual files. Postgres stores metadata (`CatalogUpload`, `ArchivoAdjunto`).

### Multi-Tenancy
*   All core tables (`User`, `PymePedido`, `MunicipioTicket`, `CatalogoItem`) are strictly scoped by `tenant_id`.
*   `TenantProfile` configures feature flags (`send_buyer_email`, `send_dispatch_whatsapp`) and theme settings.

### Key Modules

#### A. Analytics V2 (`/api/analytics`)
*   **Model:** `AnalyticsEvent` (Event Log).
*   **Service:** `AnalyticsService` aggregates data on-demand (or via jobs).
*   **Endpoints:**
    *   `GET /summary`: KPIs (volumen, conversion, SLA), Time Series.
    *   `GET /heatmap`: Geospatial density (MapLibre ready).
    *   `GET /insights`: AI-generated trends.

#### B. Universal Catalog Import (`/api/catalog/import`)
*   **Flow:** Upload -> Parse (Preview) -> User Edit -> Commit.
*   **Service:** `CatalogImportService` + `CatalogProcessor` strategy pattern (Bodega, Corralon, Generic).
*   **Sync:** Commit triggers async `catalog_vector_sync` to update Qdrant.

#### C. Orders & Notifications
*   **Model:** `PymePedido` + `OrderEvent` (Audit trail).
*   **Dispatcher:** `NotificationDispatcher` centralizes Email/WhatsApp logic.
*   **Smart Context:** `UserContextService` injects "Last Order" and "Points" into the AI prompt (`gpt-4o`) to enable "Repeat Order" flows.

#### D. Live Voting (`/api/public/encuestas`)
*   **Feature:** "YouTube-style" real-time results.
*   **Endpoint:** `GET /<slug>/live-results` returns aggregate counts for frontend animations.

## 2. Frontend Implementation Checklist

The frontend developer should build the following components consuming the new APIs.

### 1. Analytics Dashboard
*   **Route:** `/:tenant/analytics`
*   **Components:**
    *   `OverviewDashboard`: Cards for KPIs + Recharts for Volume/Categories.
    *   `HeatmapDashboard`: MapLibre GL instance rendering points/heatmap layer from `/api/analytics/heatmap`.
    *   `InsightsPanel`: List of AI suggestions.
*   **Reference:** See `FRONTEND_ANALYTICS_GUIDE.md`.

### 2. Import Wizard
*   **Route:** `/:tenant/catalog/import`
*   **Steps:**
    1.  **Upload:** File picker (PDF/Excel) -> `POST /upload`.
    2.  **Preview:** Editable Table of detected items.
    3.  **Commit:** "Save" button -> `POST /commit`.
*   **Reference:** See `FRONTEND_IMPORT_WIZARD.md`.

### 3. Unified Admin Panel
*   **Tickets:** `/admin/tickets` (Kanban/List). Filter by Category. Map View.
*   **Orders:** `/admin/orders`. Status toggle triggers `NotificationDispatcher` emails.

### 4. Public Voting Page
*   **Route:** `/e/:slug`
*   **Logic:**
    *   Render poll options.
    *   On vote submit -> Show "Results" tab with animated bars (fetch `/live-results` every 5s).

## 3. Configuration & Environment

*   **Flags:** `FEATURE_ENCUESTAS=1`, `AUTO_ASSIGN_TICKETS=1`.
*   **LLM:** `gpt-4o` is used for high-value extraction (orders, claims), `gpt-4o-mini` for chatter.
*   **Storage:** Ensure `GOOGLE_APPLICATION_CREDENTIALS` or Cloudinary keys are set for file uploads.

## 4. Next Steps for Developer

1.  **Frontend:** Copy the React components from the generated MD guides.
2.  **Testing:** Verify the full flow: Import a Catalog -> Search in Chat (Qdrant) -> Create Order (Postgres) -> Check Analytics (Event Log).
