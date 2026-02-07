import os
import base64
import requests
import logging
import json
import re
from openai import OpenAI

logger = logging.getLogger(__name__)

def encode_image_to_base64(image_path_or_url: str) -> str | None:
    """
    Encodes a local image file or an image from a URL into a base64 string.
    """
    try:
        if image_path_or_url.startswith("http://") or image_path_or_url.startswith("https://"):
            response = requests.get(image_path_or_url)
            response.raise_for_status()
            return base64.b64encode(response.content).decode('utf-8')
        else:
            with open(image_path_or_url, "rb") as image_file:
                return base64.b64encode(image_file.read()).decode('utf-8')
    except Exception as e:
        logger.error(f"Error encoding image {image_path_or_url}: {e}", exc_info=True)
        return None

def analizar_imagen_openai(image_path_or_url: str, prompt: str) -> dict | None:
    """
    Analyzes an image using OpenAI's Vision API and returns a structured response.
    Parses JSON from the LLM output.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY not found in environment variables.")
        return None

    base64_image = encode_image_to_base64(image_path_or_url)
    if not base64_image:
        return None

    client = OpenAI(api_key=api_key)

    try:
        response = client.chat.completions.create(
            model="gpt-4o",  # Upgraded to gpt-4o for better vision capabilities
            messages=[
                {
                    "role": "system",
                    "content": "You are a helpful assistant that analyzes images and outputs strict JSON."
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt + "\n\nReturn ONLY valid JSON without markdown formatting."},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        },
                    ],
                }
            ],
            max_tokens=1000,
            response_format={"type": "json_object"}, # Force JSON mode
        )
        message_content = response.choices[0].message.content
        logger.info(f"OpenAI Vision API response: {message_content[:200]}...")

        # Clean potential markdown
        cleaned_content = message_content.replace("```json", "").replace("```", "").strip()

        try:
            return json.loads(cleaned_content)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON from OpenAI response: {e}. Content: {cleaned_content}")
            return {"raw_response": cleaned_content}

    except Exception as e:
        logger.error(f"Error calling OpenAI Vision API: {e}", exc_info=True)
        return None

def analizar_imagen_con_fallback(image_path_or_url: str, prompt: str) -> dict | None:
    """
    Analyzes an image using a fallback mechanism: OpenAI first, then others if needed.
    """
    logger.info(f"Analyzing image: {image_path_or_url}")

    # 1. Try OpenAI
    try:
        result = analizar_imagen_openai(image_path_or_url, prompt)
        if result:
            return result
    except Exception as e:
        logger.error(f"OpenAI Vision analysis failed: {e}", exc_info=True)

    # 2. Fallback (Placeholder for future)
    logger.warning("Falling back to placeholder/legacy logic...")
    return None

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    # Testing stub
    print("Multimodal analyzer structure updated.")
