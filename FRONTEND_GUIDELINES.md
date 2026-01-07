# Frontend Guidelines and Handoff

This document outlines recent backend changes and provides guidance for the frontend team to address related issues.

## Backend Fixes Implemented

1.  **Admin Order Visibility:** A critical bug was fixed where new orders created via WhatsApp were not appearing in the tenant admin panel. The root cause was that orders were being saved to an old, deprecated table (`PymePedido`) instead of the correct, tenant-aware table (`MarketOrder`). This has been corrected. All new orders will now correctly appear in the admin panel.

2.  **Tenant Plan Status:** A bug was fixed where the subscription plan for a tenant (e.g., "Gratis", "Full") was not updating correctly in the admin panel after being changed by a super admin. The frontend was reading the plan from the logged-in user's record, which was not always synchronized. The API now correctly returns the plan directly from the tenant's profile, ensuring the displayed plan is always accurate.

## New Feature: Order Status Updates

The backend now supports updating an order's status and notifying the customer via WhatsApp.

**API Endpoint:** `PUT /api/admin/tenants/<slug>/orders/<order_id>`

**Request Body:**

```json
{
  "status": "confirmed"
}
```

-   `status`: Can be set to `"confirmed"`, `"in_progress"`, `"shipped"`, `"delivered"`, or `"cancelled"`.

**Functionality:**

-   When the status of an order is updated via this endpoint, the backend will automatically trigger a WhatsApp notification to the customer who placed the order (if a valid phone number is associated with the order).
-   The frontend should now implement the UI for admins to change the order status (e.g., a dropdown menu on the order details page).

## Analysis of Frontend JavaScript Errors

The following errors were reported in the browser console and appear to originate from the frontend codebase:

1.  **`TypeError: Cannot read properties of undefined (reading 'length')`**
    -   **File:** `main.js:1017`
    -   **Analysis:** This is a very common JavaScript error that occurs when trying to access the `.length` property of a variable that is `undefined`. In the context of the logs, it appears to be happening within a `.map()` function. This strongly suggests that an API endpoint is expected to return an array, but it is returning `undefined` or a JSON object that does not contain the expected array.
    -   **Likely Cause:** An API call that should return a list of items (e.g., order items, ticket comments, notifications) is failing or returning an unexpected structure. The frontend code does not have a "guard" to check if the data is a valid array before trying to map over it.
    -   **Recommendation:**
        -   Identify the component and the specific API call that triggers this error. The "ticket status page" was mentioned, so the error is likely related to fetching ticket details or comments.
        -   In the frontend code, before calling `.map()` on a variable, add a check to ensure it is an array. For example: `(myArray || []).map(...)`. This will prevent the crash by mapping over an empty array if the data is missing.
        -   Verify that the corresponding backend API is returning the data in the expected array format, even when there are no items to return (it should return `[]`, not `null` or `undefined`).

2.  **`SyntaxError: The requested module './iframe.js' does not provide an export named 'x'`**
    -   **File:** `ProactiveBubble.js:1`
    -   **Analysis:** This is an ES module import/export error. The component `ProactiveBubble.js` is trying to import a specific item named `x` from a file named `iframe.js`, but `iframe.js` does not export anything with that name.
    -   **Likely Cause:** This could be due to a typo in the import statement, a recent refactoring where the export was removed or renamed in `iframe.js`, or a problem with how the frontend build tool is bundling the modules.
    -   **Recommendation:**
        -   Inspect the file `ProactiveBubble.js` at line 1.
        -   Inspect the file `iframe.js` and check what it exports.
        -   Correct the import statement in `ProactiveBubble.js` to match the actual exports available from `iframe.js`. If the `x` export was intentionally removed, the logic in `ProactiveBubble.js` that depends on it will need to be updated or removed.

These issues are confined to the frontend codebase and will need to be addressed there. The backend fixes should resolve the data consistency problems, which may in turn alleviate some of the conditions causing these JavaScript errors.
