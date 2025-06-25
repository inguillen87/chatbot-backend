# Widget/Perfil Inteligente según estado de usuario

Este snippet muestra cómo alternar entre un perfil editable y un bloque de
información de la empresa cuando no hay usuario autenticado.

```jsx
// En el componente del avatar o menú de usuario
const isLoggedIn = !!user && !!user.token;
const isEmpresaInfoAvailable = !!empresaActual; // información cargada de la pyme o municipio

return (
  <>
    {isLoggedIn ? (
      // Usuario autenticado: se muestra el perfil editable
      <PerfilEditable user={user} />
    ) : isEmpresaInfoAvailable ? (
      // No hay usuario, pero sí datos de la empresa: solo se muestran
      // los detalles informativos, sin permitir edición
      <EmpresaInfo
        nombre={empresaActual.nombre}
        logoUrl={empresaActual.logo_url}
        rubro={empresaActual.rubro}
        direccion={empresaActual.direccion}
        telefono={empresaActual.telefono}
        web={empresaActual.web}
      />
    ) : null}
  </>
);

function EmpresaInfo({ nombre, logoUrl, rubro, direccion, telefono, web }) {
  return (
    <div className="p-4 rounded-xl bg-card border shadow-lg max-w-sm mx-auto">
      <div className="flex items-center gap-3 mb-2">
        {logoUrl && (
          <img src={logoUrl} alt="Logo" className="w-10 h-10 rounded-lg" />
        )}
        <div>
          <div className="font-bold text-lg">{nombre}</div>
          {rubro && <div className="text-xs text-muted-foreground">{rubro}</div>}
        </div>
      </div>
      <div className="text-xs text-muted-foreground">
        {direccion && <div>📍 {direccion}</div>}
        {telefono && <div>📞 {telefono}</div>}
        {web && (
          <div>
            🌐 <a href={web} target="_blank" rel="noopener noreferrer" className="underline">{web}</a>
          </div>
        )}
      </div>
    </div>
  );
}
```

Ubica esta lógica en el header o menú del avatar. Si tu menú está dentro de un
`Drawer` o `Popover`, utiliza la misma condición antes de renderizar el
contenido.
