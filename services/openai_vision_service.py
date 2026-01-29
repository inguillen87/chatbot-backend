import base64
import logging
import os
from typing import Optional, Dict, Any, List
from services.openai_bridge import get_openai_client

logger = logging.getLogger(__name__)

class OpenAIVisionService:
    def __init__(self):
        self.client = get_openai_client()

    def analyze_image(self, image_path: str, prompt: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """
        Sends an image to OpenAI GPT-4o for analysis and extraction of structured catalog data.

        Args:
            image_path: Path to the local image file.
            prompt: Optional custom prompt. If not provided, a default catalog extraction prompt is used.

        Returns:
            A list of dictionaries representing the extracted products, or None if failed.
        """
        if not self.client:
            logger.error("OpenAI client not initialized.")
            return None

        try:
            with open(image_path, "rb") as image_file:
                encoded_image = base64.b64encode(image_file.read()).decode('utf-8')

            file_ext = os.path.splitext(image_path)[1].lower().replace('.', '')
            mime_type = f"image/{file_ext if file_ext != 'jpg' else 'jpeg'}"

            default_prompt = """
            You are an expert data extraction assistant. Analyze the provided image of a product catalog or list.
            Extract all products visible in the image into a structured JSON format.

            The JSON output should be a list of objects, where each object represents a product with the following keys:
            - 'nombre': Name of the product (string)
            - 'descripcion': Description or details (string, empty if not found)
            - 'precio': Price as a string (e.g., "$100", "10.50 USD")
            - 'sku': SKU or code (string, empty if not found)
            - 'marca': Brand name (string, empty if not found)
            - 'categoria': Category (string, empty if not found)
            - 'unidad': Unit (e.g., "kg", "pack", "unidades") (string, empty if not found)
            - 'cantidad': Stock quantity if visible (string, empty if not found)

            If the image contains a menu, treat items as products.
            Return ONLY the raw JSON array. Do not include markdown formatting (like ```json).
            """

            final_prompt = prompt or default_prompt

            response = self.client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a helpful assistant that extracts structured data from images."
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": final_prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{encoded_image}"
                                }
                            }
                        ]
                    }
                ],
                max_tokens=4000
            )

            content = response.choices[0].message.content

            # Clean up potential markdown formatting
            if content.startswith("```json"):
                content = content[7:]
            if content.endswith("```"):
                content = content[:-3]

            import json
            try:
                data = json.loads(content.strip())
                if isinstance(data, list):
                    return data
                elif isinstance(data, dict) and "productos" in data:
                    return data["productos"]
                else:
                    logger.warning(f"Unexpected JSON structure from OpenAI Vision: {type(data)}")
                    return []
            except json.JSONDecodeError as e:
                logger.error(f"Failed to decode JSON from OpenAI Vision response: {e}. Content: {content}")
                return None

        except Exception as e:
            logger.error(f"Error calling OpenAI Vision API: {e}", exc_info=True)
            return None
