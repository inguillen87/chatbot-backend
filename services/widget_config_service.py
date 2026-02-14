import logging
import re
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from database import db
from models import TenantProfile, TenantWidgetConfig, WidgetSettings, User

logger = logging.getLogger(__name__)

class WidgetConfigService:
    """
    Service for managing SaaS Widget Configurations.
    Handles the Draft/Live workflow and backwards compatibility.
    """

    DEFAULT_CONFIG = {
        "appearance": {
            "primaryColor": "#007aff",
            "secondaryColor": "#005bb5",
            "position": "bottom-right",
            "bubbleShape": "round",
            "welcomeTitle": "Asistente Virtual",
            "welcomeSubtitle": "¡Hola! ¿En qué puedo ayudarte hoy?",
            "logoUrl": "",
            "logoAnimation": "pulse"
        },
        "behavior": {
            "defaultOpen": False,
            "soundEnabled": True,
            "showTyping": True
        },
        "ux": {
            "preset": "premium",
            "motion_level": "balanced",
            "glassmorphism": True,
            "logo_ring": True,
            "gradient_start": "#0f172a",
            "gradient_end": "#007aff",
        },
        "domains": [],  # Allowed domains for CORS/Security (future use)
        "channels": {
            "whatsapp": {"enabled": False, "number": ""},
            "messenger": {"enabled": False, "pageId": ""}
        }
    }

    @staticmethod
    def get_public_config(tenant_slug: str) -> Dict[str, Any]:
        """
        Retrieves the LIVE configuration for a tenant.
        Falls back to legacy WidgetSettings/TenantProfile if no SaaS config exists.
        """
        tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()
        if not tenant:
            return None

        # Try to get SaaS config
        saas_config = TenantWidgetConfig.query.filter_by(tenant_id=tenant.id).first()

        if saas_config and saas_config.config_live:
            # Merge with defaults to ensure all keys exist
            return WidgetConfigService._merge_with_defaults(saas_config.config_live)

        # Fallback: Migrate/Construct from legacy on the fly
        return WidgetConfigService._construct_legacy_fallback(tenant)

    @staticmethod
    def get_admin_config(tenant_slug: str) -> Dict[str, Any]:
        """
        Retrieves the full configuration state (Draft & Live) for the admin panel.
        """
        tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()
        if not tenant:
            return None

        saas_config = TenantWidgetConfig.query.filter_by(tenant_id=tenant.id).first()

        if not saas_config:
            # Initialize if not exists
            initial_config = WidgetConfigService._construct_legacy_fallback(tenant)
            saas_config = TenantWidgetConfig(
                tenant_id=tenant.id,
                config_live=initial_config,
                config_draft=initial_config
            )
            db.session.add(saas_config)
            db.session.commit()

        return {
            "live": WidgetConfigService._merge_with_defaults(saas_config.config_live),
            "draft": WidgetConfigService._merge_with_defaults(saas_config.config_draft),
            "last_published_at": saas_config.last_published_at.isoformat() if saas_config.last_published_at else None,
            "published_by": saas_config.published_by
        }

    @staticmethod
    def update_draft(tenant_slug: str, draft_payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Updates the DRAFT configuration.
        """
        tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()
        if not tenant:
            raise ValueError("Tenant not found")

        # Validate
        clean_config = WidgetConfigService._validate_and_sanitize(draft_payload)

        saas_config = TenantWidgetConfig.query.filter_by(tenant_id=tenant.id).first()
        if not saas_config:
            # Should exist if get_admin_config was called, but handle creation
            initial_legacy = WidgetConfigService._construct_legacy_fallback(tenant)
            saas_config = TenantWidgetConfig(
                tenant_id=tenant.id,
                config_live=initial_legacy,
                config_draft=clean_config
            )
            db.session.add(saas_config)
        else:
            saas_config.config_draft = clean_config

        db.session.commit()
        return clean_config

    @staticmethod
    def publish_draft(tenant_slug: str, user_id: int) -> Dict[str, Any]:
        """
        Promotes Draft to Live.
        """
        tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()
        if not tenant:
            raise ValueError("Tenant not found")

        saas_config = TenantWidgetConfig.query.filter_by(tenant_id=tenant.id).first()
        if not saas_config:
            raise ValueError("Configuration not initialized")

        # Promote
        saas_config.config_live = saas_config.config_draft
        saas_config.last_published_at = datetime.now(timezone.utc)
        saas_config.published_by = user_id

        db.session.commit()
        return saas_config.config_live

    @staticmethod
    def _validate_and_sanitize(config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Sanitizes input JSON.
        """
        # Copy to avoid mutation
        import copy
        clean = copy.deepcopy(WidgetConfigService.DEFAULT_CONFIG)

        # Deep merge/override logic could be more complex,
        # for now we allow overwriting sections if provided.

        if "appearance" in config and isinstance(config["appearance"], dict):
            for k, v in config["appearance"].items():
                if k in clean["appearance"]:
                    # Basic validation
                    if "Color" in k and not re.match(r"^#[0-9a-fA-F]{3,8}$", str(v)):
                        continue # Invalid color, keep default or ignore
                    clean["appearance"][k] = v

        if "behavior" in config and isinstance(config["behavior"], dict):
            for k, v in config["behavior"].items():
                if k in clean["behavior"]:
                    clean["behavior"][k] = bool(v)

        if "ux" in config and isinstance(config["ux"], dict):
            for k, v in config["ux"].items():
                if k not in clean["ux"]:
                    continue
                if k in {"glassmorphism", "logo_ring"}:
                    clean["ux"][k] = bool(v)
                elif k in {"gradient_start", "gradient_end"}:
                    if re.match(r"^#[0-9a-fA-F]{3,8}$", str(v)):
                        clean["ux"][k] = str(v)
                else:
                    clean["ux"][k] = str(v).strip()

        if "domains" in config and isinstance(config["domains"], list):
            clean["domains"] = [str(d) for d in config["domains"]]

        if "channels" in config and isinstance(config["channels"], dict):
            clean["channels"].update(config["channels"])

        return clean

    @staticmethod
    def _merge_with_defaults(config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Merges provided config with defaults to ensure schema consistency.
        """
        import copy
        merged = copy.deepcopy(WidgetConfigService.DEFAULT_CONFIG)

        if not config:
            return merged

        # Recursive update would be better, but simple section update is safer for now
        if "appearance" in config:
            merged["appearance"].update(config["appearance"])
        if "behavior" in config:
            merged["behavior"].update(config["behavior"])
        if "ux" in config:
            merged["ux"].update(config["ux"])
        if "domains" in config:
            merged["domains"] = config["domains"]
        if "channels" in config:
            merged["channels"].update(config["channels"])

        return merged

    @staticmethod
    def _construct_legacy_fallback(tenant: TenantProfile) -> Dict[str, Any]:
        """
        Builds a config object from legacy fields (TenantProfile, WidgetSettings).
        """
        defaults = WidgetConfigService.DEFAULT_CONFIG.copy()
        appearance = defaults["appearance"].copy()

        # Try WidgetSettings
        ws = tenant.widget_settings
        if ws:
            appearance["primaryColor"] = ws.primary_color or appearance["primaryColor"]
            appearance["secondaryColor"] = ws.secondary_color or appearance["secondaryColor"]
            appearance["welcomeTitle"] = ws.welcome_title or appearance["welcomeTitle"]
            appearance["welcomeSubtitle"] = ws.welcome_subtitle or appearance["welcomeSubtitle"]
            appearance["position"] = ws.position or appearance["position"]
            appearance["bubbleShape"] = ws.bubble_shape or appearance["bubbleShape"]
            appearance["logoUrl"] = ws.avatar_url or appearance["logoUrl"]

            # Check theme_config if exists
            if ws.theme_config:
                # Merge logic here if needed
                pass

        # Try TenantProfile directly
        if tenant.logo_url:
            appearance["logoUrl"] = tenant.logo_url

        # Construct result
        return {
            "appearance": appearance,
            "behavior": defaults["behavior"],
            "domains": [],
            "channels": defaults["channels"]
        }
