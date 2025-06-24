# React login troubleshooting

If navigating to the login page crashes the app with an `ErrorBoundary` showing a generic "Error", it usually means that the module is being loaded with `React.lazy()` but the import fails.

Things to verify:

1. **Check the file path and casing.** Make sure the component exists in the exact path used by `React.lazy(() => import("./LoginForm"))`. On Linux servers the file name is case‑sensitive (`LoginForm.tsx` vs `loginform.tsx`).
2. **Wrap the lazy component with `<Suspense>`.** Without a `Suspense` fallback React will throw an error when the lazy import resolves.

```tsx
import React, { Suspense } from 'react';
const LoginForm = React.lazy(() => import('./LoginForm'));

export default function AuthPage() {
  return (
    <Suspense fallback={<div>Loading…</div>}>
      <LoginForm />
    </Suspense>
  );
}
```

Running the frontend in development mode (`npm start` or `npm run dev`) will show the full stack trace so you can pinpoint the missing file or typo. When the build is minified the line numbers no longer match and only a generic "Error" is shown.
