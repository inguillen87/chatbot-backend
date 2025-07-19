import os
import json
import logging
from typing import Optional

from google.oauth2 import service_account
from google.auth.exceptions import DefaultCredentialsError

logger = logging.getLogger(__name__)

def get_google_credentials() -> Optional[service_account.Credentials]:
    """
    Loads Google Cloud credentials from various sources in a prioritized order.

    The order of priority is:
    1. GOOGLE_CREDENTIALS_JSON environment variable (containing the JSON string).
    2. GOOGLE_APPLICATION_CREDENTIALS environment variable (containing the file path).
    3. A list of common fallback file paths.
    4. Application Default Credentials (ADC) as a final fallback.

    Returns:
        A google.oauth2.service_account.Credentials object if found, otherwise None.
    """
    credentials = None
    credentials_info = None
    source = "Not found"

    # 1. Check for JSON string in env var
    google_creds_json_str = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if google_creds_json_str:
        try:
            credentials_info = json.loads(google_creds_json_str)
            source = "GOOGLE_CREDENTIALS_JSON environment variable"
            logger.info(f"✅ Loading Google credentials from {source}.")
        except json.JSONDecodeError as e:
            logger.error(f"❌ Invalid JSON in GOOGLE_CREDENTIALS_JSON: {e}")
            return None

    # 2. Check for file path in env var
    if not credentials_info:
        gac_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if gac_path and os.path.exists(gac_path):
            source = f"GOOGLE_APPLICATION_CREDENTIALS env var path: {gac_path}"
            logger.info(f"✅ Loading Google credentials from {source}.")
            with open(gac_path, "r", encoding="utf-8") as f:
                credentials_info = json.load(f)
        elif gac_path:
             logger.warning(f"⚠️ GOOGLE_APPLICATION_CREDENTIALS pointed to a file that does not exist: {gac_path}")


    # 3. Check common fallback paths
    if not credentials_info:
        fallback_paths = [
            "/data/vision_service_key.json",       # Added for Vision API credentials
            "/app/google_service_key.json",        # Local dev (mounted at root)
            "/app/instance/google-credentials.json", # Local dev (in instance folder)
            "/etc/secrets/google_service_key.json"   # Production (Render secrets)
        ]
        for path in fallback_paths:
            if os.path.exists(path):
                source = f"fallback path: {path}"
                logger.info(f"✅ Loading Google credentials from {source}.")
                with open(path, "r", encoding="utf-8") as f:
                    credentials_info = json.load(f)
                break

    if credentials_info:
        try:
            credentials = service_account.Credentials.from_service_account_info(credentials_info)
            logger.info(f"✅ Successfully created credentials object from '{source}'.")
            return credentials
        except Exception as e:
            logger.error(f"❌ Failed to create credentials from info loaded from '{source}': {e}")
            return None

    # 4. Final fallback to Application Default Credentials (ADC)
    logger.warning("⚠️ No explicit credentials found. Attempting to use Application Default Credentials (ADC).")
    try:
        import google.auth
        credentials, project_id = google.auth.default()
        logger.info(f"✅ Successfully loaded Application Default Credentials. Project ID: {project_id or 'Not determined'}")
        return credentials
    except DefaultCredentialsError:
        logger.error("❌ ADC not found. Could not automatically determine credentials.")
        return None
    except Exception as e:
        logger.error(f"❌ An unexpected error occurred during ADC fallback: {e}")
        return None
