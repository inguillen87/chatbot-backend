# Frontend Integration Tasks

## 1. Login Redirect Update
**Requirement:** When a user with the role `admin` (or `superadmin`) logs in, they should be redirected to `/perfil` instead of `/admin` or `/dashboard`.
**Reason:** The user prefers immediate access to personal settings and quick actions available in the profile section.

**Action:**
Update the login success handler (in `Login.js`, `AuthContext.js`, or similar) to check the user's role and redirect accordingly.

**Example Logic (pseudo-code):**
```javascript
const handleLoginSuccess = (response) => {
    const { token, user } = response;
    // ... store token ...

    if (user.rol === 'admin' || user.rol === 'superadmin') {
        history.push('/perfil');
    } else {
        // Default redirect
        history.push('/dashboard');
    }
}
```

## 2. Verify "Disponibilidad" in Product Catalog
**Requirement:** The backend has added a `disponible` field to `CatalogoItem`.
**Action:**
- Ensure the Product Management UI allows toggling this field.
- Ensure the Public Catalog (Marketplace) filters out products where `disponible` is `false`.

## 3. Employee Permissions
**Requirement:** Legacy admins might encounter 403 errors if accessing tenant-specific resources without explicit `tenant_id` linkage.
**Backend Fix:** The backend now attempts to authorize based on `municipio_id`/`pyme_id` ownership if `tenant_id` is missing.
**Frontend Action:**
- If 403 errors persist for legacy users, ensure the `X-Tenant` header is being sent with requests if the user is in a tenant context.
