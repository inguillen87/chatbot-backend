"""Backend-owned plain-text presentation. Does not grant rights or write data."""
import re
CONFIG_KEY = 'organization_branding_workflow_copy'
CONTRACT = 'organization.branding_workflow_ui.v1'
TEXTS = {'studio_label': 'Estudio de marca',
 'eyebrow': 'Identidad · Personalización Full',
 'version_label': 'Versión {version}',
 'toggle_label': 'Usar mi paleta en el espacio',
 'presets_label': 'Paletas iniciales',
 'primary_label': 'Color principal',
 'accent_label': 'Color de acento',
 'primary_picker': 'Principal: selector de color',
 'accent_picker': 'Acento: selector de color',
 'input_hint': 'El texto sobre cada color se ajusta automáticamente a blanco o negro. No se aceptan estilos '
               'ni scripts personalizados.',
 'primary_contrast': 'Principal: {contrast}:1',
 'accent_contrast': 'Acento: {contrast}:1',
 'invalid_colors': 'Completá ambos colores con formato #RRGGBB para previsualizar o publicar.',
 'contrast_scope': 'La relación corresponde sólo al texto sobre estas muestras; no es una certificación de '
                   'accesibilidad del sitio completo.',
 'verifying': 'Verificando…',
 'publish_action': 'Publicar paleta',
 'refresh_action': 'Revisar versión actual',
 'discard_action': 'Descartar borrador',
 'draft_pending': 'Borrador con cambios sin publicar',
 'draft_pristine': 'La paleta coincide con la última versión consultada',
 'comparison_label': 'Cambios de paleta',
 'comparison_review': 'Versión actual y tu borrador',
 'comparison_publish': 'Resumen de publicación',
 'application_label': 'Aplicación',
 'enabled_label': 'Activada',
 'disabled_label': 'Desactivada',
 'changed_label': 'Cambiará',
 'unchanged_label': 'Sin cambios',
 'saved_label': 'Guardado',
 'proposed_label': 'Propuesto',
 'draft_hint': 'El borrador permanece sólo en esta pantalla. No se guarda automáticamente al salir ni se '
               'recupera después de cerrar.',
 'review_label': 'Comparar paletas',
 'review_note': 'Versión guardada {version}. Compará los tres valores antes de elegir. Elegir no publica.',
 'keep_draft_action': 'Conservar mi borrador',
 'use_saved_action': 'Usar paleta guardada',
 'preview_title': 'Vista previa · Sin publicar',
 'preview_mobile': 'Vista móvil',
 'preview_desktop': 'Vista escritorio',
 'preview_light': 'Ver claro',
 'preview_dark': 'Ver oscuro',
 'preview_invalid': 'Colores incompletos · muestra neutra',
 'preview_enabled': 'Paleta del borrador · aún sin publicar',
 'preview_disabled': 'Paleta desactivada · muestra neutra',
 'preview_logo_alt': 'Logo institucional de vista previa',
 'organization_fallback': 'Organización',
 'initials_fallback': 'OR',
 'preview_badge': 'Tu espacio de atención',
 'preview_heading': 'Una identidad, todos tus equipos',
 'preview_body': 'Vista de presentación. No contiene métricas, mensajes ni datos de clientes.',
 'preview_main_action': 'Acción principal',
 'preview_accent': 'Identidad del espacio',
 'preview_profile_hint': 'Nombre y logo pertenecen al perfil institucional. Publicar la paleta no guarda '
                         'cambios pendientes de esos campos.',
 'history_heading': 'Historial de paleta · {count} versiones anteriores',
 'history_row': 'Versión {version} · {primary} · {accent} · {state}',
 'restore_action': 'Restaurar versión {version}',
 'history_empty': 'Todavía no hay una versión anterior publicada.',
 'history_hint': 'Se conservan hasta diez versiones anteriores. Restaurar publica una nueva versión; no '
                 'borra auditoría ni modifica otras integraciones.',
 'discard_title': '¿Descartar el borrador de paleta?',
 'publish_title': '¿Publicar esta paleta?',
 'restore_title': '¿Restaurar la versión {version}?',
 'discard_description': 'Se recuperará la última paleta consultada. No se enviará ningún cambio al servidor '
                        'ni se modificarán nombre o logo.',
 'publish_description': 'Organización: {organization}. Cambiarán únicamente los colores de las superficies '
                        'indicadas. No se envían mensajes ni se modifican planes o dominios.',
 'restore_draft_warning': 'Restaurar reemplazará también tu borrador de paleta si el servidor confirma la '
                          'publicación.',
 'cancel_action': 'Seguir revisando',
 'confirm_discard': 'Confirmar descarte',
 'confirm_publish': 'Confirmar publicación',
 'publish_saved': 'La paleta quedó publicada y confirmada por el servidor.',
 'publish_unchanged': 'La paleta ya coincidía con la versión guardada.',
 'conflict_error': 'Otra persona cambió la marca. Revisá la versión actual sin perder tu borrador.',
 'publish_unconfirmed': 'No pudimos confirmar la publicación. Consultá el estado antes de reintentar.'}


def build_brand_workflow_ui(tenant):
    texts = dict(TEXTS)
    config = getattr(tenant, 'configuracion', None)
    overrides = config.get(CONFIG_KEY, {}) if isinstance(config, dict) else {}
    if isinstance(overrides, dict):
        for key, original in TEXTS.items():
            candidate = overrides.get(key)
            if not isinstance(candidate, str) or not 1 <= len(candidate.strip()) <= 600:
                continue
            if re.search(r'[\x00-\x1f\x7f<>]', candidate):
                continue
            if set(re.findall(r'\{([^{}]+)\}', candidate)) != set(re.findall(r'\{([^{}]+)\}', original)):
                continue
            texts[key] = candidate.strip()
    return {'contract_version': CONTRACT, 'texts': texts}
