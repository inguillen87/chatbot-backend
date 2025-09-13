import os
import base64
import requests
import logging
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
            if image_path_or_url.startswith("/"):
                from app import app
                local_path = os.path.join(app.root_path, image_path_or_url.lstrip("/"))
                if os.path.exists(local_path):
                    image_path_or_url = local_path
                else:
                    base = app.config.get("APP_PUBLIC_BASE_URL")
                    if base:
                        response = requests.get(base.rstrip("/") + image_path_or_url)
                        response.raise_for_status()
                        return base64.b64encode(response.content).decode('utf-8')
                    else:
                        raise FileNotFoundError(local_path)
            with open(image_path_or_url, "rb") as image_file:
                return base64.b64encode(image_file.read()).decode('utf-8')
    except Exception as e:
        logger.error(f"Error encoding image {image_path_or_url}: {e}", exc_info=True)
        return None

def analizar_imagen_openai(image_path_or_url: str, prompt: str) -> dict | None:
    """
    Analyzes an image using OpenAI's Vision API and returns a structured response.
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
            model="gpt-4-turbo",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        },
                    ],
                }
            ],
            max_tokens=300,
        )
        message_content = response.choices[0].message.content
        logger.info(f"OpenAI Vision API response: {message_content}")
        # Further processing to extract JSON from the response would go here
        return {"raw_response": message_content}
    except Exception as e:
        logger.error(f"Error calling OpenAI Vision API: {e}", exc_info=True)
        return None

def analizar_imagen_con_fallback(image_path_or_url: str, prompt: str) -> dict | None:
    """
    Analyzes an image using a fallback mechanism: OpenAI first, then Google Vision.
    """
    logger.info(f"Analyzing image with fallback: {image_path_or_url}")

    # 1. Try OpenAI
    try:
        logger.info("Attempting analysis with OpenAI Vision...")
        result = analizar_imagen_openai(image_path_or_url, prompt)
        if result:
            logger.info("OpenAI Vision analysis successful.")
            return result
    except Exception as e:
        logger.error(f"OpenAI Vision analysis failed: {e}", exc_info=True)

    # 2. Fallback to Google Vision (Placeholder)
    logger.warning("Falling back to Google Vision...")
    # TODO: Implement Google Vision analysis logic here
    # google_vision_result = analizar_imagen_google(image_path_or_url)
    # if google_vision_result:
    #     # This would likely require a second LLM call to interpret the labels
    #     return interpret_google_vision_labels(google_vision_result)

    logger.error("All image analysis providers failed.")
    return None

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    # This test requires a valid image URL or local path and API keys.
    # test_image = "https://www.lahoralibre.com.ar/wp-content/uploads/2022/04/bache-de-calle-tierra-1.jpg" # Example of a pothole
    # test_prompt = "Analyze this image from a municipality's point of view. Identify the main issue. Return a JSON object with 'intent' and 'data' keys. The intent should be a specific action like 'crear_reclamo'. The data object should contain a 'categoria' and a 'descripcion'."
    # result = analizar_imagen_con_fallback(test_image, test_prompt)
    # if result:
    #     print("Analysis result:", result)
    # else:
    #     print("Analysis failed.")
    print("Multimodal analyzer structure created.")
