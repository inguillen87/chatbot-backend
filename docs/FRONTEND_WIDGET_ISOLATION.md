# Frontend Widget Isolation Guide

## Problem: CSS Leakage
The Chatboc Widget (`widget.js` / `widget-chat`) currently injects styles directly into the host page's DOM. This causes conflicts, such as the widget's Dark Mode global styles overriding the host site's `body` background or typography.

## Solution: Shadow DOM Isolation

The frontend team must encapsulate the widget's UI within a **Shadow DOM**. This provides a boundary that prevents styles from leaking out (affecting the host) and external styles from leaking in (affecting the widget).

### Implementation Steps

1.  **Mount Point**:
    Instead of appending the widget container directly to `document.body`, create a host element and attach a Shadow Root.

    ```javascript
    // Create Host
    const hostElement = document.createElement('div');
    hostElement.id = 'chatboc-widget-host';
    document.body.appendChild(hostElement);

    // Attach Shadow DOM
    const shadowRoot = hostElement.attachShadow({ mode: 'open' });
    ```

2.  **Style Injection**:
    All CSS required by the widget (Tailwind, custom themes, fonts) must be injected *inside* the Shadow Root, not the `document.head`.

    ```javascript
    const styleTag = document.createElement('style');
    styleTag.textContent = `
      /* Widget CSS here */
      :host {
        all: initial; /* Reset inherited properties */
        font-family: system-ui, -apple-system, sans-serif;
      }
      .widget-container {
        /* Local styles */
      }
    `;
    shadowRoot.appendChild(styleTag);
    ```

3.  **React / Vue Mounting**:
    If using a framework, mount the application instance to a root element *inside* the shadow root.

    ```javascript
    const appRoot = document.createElement('div');
    shadowRoot.appendChild(appRoot);
    // React example
    const root = ReactDOM.createRoot(appRoot);
    root.render(<App />);
    ```

### Alternative: Scoped CSS (If Shadow DOM is not feasible)

If Shadow DOM cannot be used (e.g., due to specific library constraints), you **MUST** namespace all CSS classes and use high-specificity selectors.

1.  **Prefix Classes**:
    Prefix all classes with `cb-` (e.g., `.cb-btn`, `.cb-card`).

2.  **Avoid Global Selectors**:
    **NEVER** use generic tag selectors like `body`, `html`, `p`, `h1`, or `button` in the widget CSS. Always scope them under a container ID.

    ```css
    /* BAD */
    body {
        background-color: #1a1a1a;
        color: white;
    }

    /* GOOD */
    #chatboc-widget-container {
        background-color: #1a1a1a;
        color: white;
    }
    #chatboc-widget-container h1 {
        font-size: 2rem;
    }
    ```

3.  **Reset Host Styles**:
    Apply a CSS reset only to the widget container to prevent host styles from affecting the widget.

    ```css
    #chatboc-widget-container * {
        box-sizing: border-box;
    }
    ```

## Critical Check
Before deploying, ensure that toggling "Dark Mode" in the widget **does not** change the `background-color` or `color` of the host page's `<body>` element.
