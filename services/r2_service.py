import os
import logging
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from urllib.parse import quote

logger = logging.getLogger(__name__)

class R2Service:
    def __init__(self):
        self.endpoint_url = os.environ.get("R2_ENDPOINT_URL")
        self.access_key_id = os.environ.get("R2_ACCESS_KEY_ID")
        self.secret_access_key = os.environ.get("R2_SECRET_ACCESS_KEY")
        self.bucket_name = os.environ.get("R2_BUCKET_NAME")
        self.public_base_url = os.environ.get("R2_PUBLIC_BASE_URL")
        self.region_name = os.environ.get("R2_REGION", "auto")

        self.client = None
        if self.endpoint_url and self.access_key_id and self.secret_access_key:
            try:
                self.client = boto3.client(
                    's3',
                    endpoint_url=self.endpoint_url,
                    aws_access_key_id=self.access_key_id,
                    aws_secret_access_key=self.secret_access_key,
                    region_name=self.region_name,
                    config=Config(signature_version='s3v4')
                )
                logger.info("R2 Client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize R2 client: {e}")

    def upload_file(self, file_obj, filename, content_type, tenant_slug, context_type="general"):
        """
        Uploads a file to Cloudflare R2.

        Args:
            file_obj: File-like object (bytes)
            filename: Original filename
            content_type: MIME type of the file
            tenant_slug: Slug of the tenant (for folder structure)
            context_type: Type of content (e.g., 'catalogos', 'reclamos', 'logos', 'eventos')

        Returns:
            str: Public URL of the uploaded file, or None if upload fails.
        """
        if not self.client or not self.bucket_name:
            logger.warning("R2 is not configured or initialized.")
            return None

        # Determine prefix based on context
        # Format: <context_type_plural>/<tenant_slug>/<sub_folder>/<filename>
        # User requested: pymes/<slug>/catalogos/... or municipios/<slug>/reclamos/...
        # We need to map 'context_type' to this structure.

        # Simplified mapping logic based on user request:
        # If context_type is explicitly 'catalogo', 'logo', etc. we use that.
        # But we also need 'pymes' vs 'municipios'.
        # For now, let's assume the caller passes the full prefix or we build it here if we have enough info.
        # The user example: pymes/<tenant_slug>/catalogos/...

        # To make it flexible, let's assume the caller constructs the 'folder' part
        # or we accept a 'folder_prefix' argument.
        # But the method signature I proposed has `tenant_slug` and `context_type`.

        # Let's clean the filename first
        clean_filename = filename.strip()

        # Construct the key (path)
        # key = f"{folder_prefix}/{clean_filename}"
        # Wait, the user said: "pymes/<tenant_slug>/catalogos/..."
        # I'll rely on the caller to pass the correct 'prefix' logic if possible,
        # or implement a helper to build it.
        # Let's change the signature slightly to be more generic, or include a helper.

        # Actually, let's just take `key_name` as an argument to be fully flexible,
        # and a helper method `generate_key` to build it.
        pass

    def upload_file_with_key(self, file_obj, key, content_type):
        """
        Uploads a file with a specific key.
        """
        if not self.client or not self.bucket_name:
            return None

        try:
            extra_args = {
                'ContentType': content_type,
                # 'CacheControl': 'public, max-age=31536000' # Ideal for static assets
            }

            # Add cache control for images/static assets
            if content_type.startswith('image/') or content_type == 'application/pdf':
                extra_args['CacheControl'] = 'public, max-age=31536000'

            self.client.upload_fileobj(
                file_obj,
                self.bucket_name,
                key,
                ExtraArgs=extra_args
            )

            # Generate Public URL
            # Quote the key to handle spaces and special chars
            safe_key = quote(key)
            if self.public_base_url:
                return f"{self.public_base_url}/{safe_key}"
            else:
                # Fallback to endpoint if no custom domain (not requested but safe)
                # But user specifically set R2_PUBLIC_BASE_URL.
                return None

        except (BotoCoreError, ClientError) as e:
            logger.error(f"R2 Upload Failed for key {key}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error uploading to R2: {e}")
            return None

# Singleton instance
r2_service = R2Service()
