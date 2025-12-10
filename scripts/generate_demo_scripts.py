import json
import os

def generate_scripts():
    rubros_dir = "data/pyme/rubros"
    scripts_dir = "data/demo_scripts"
    os.makedirs(scripts_dir, exist_ok=True)

    for entry in os.listdir(rubros_dir):
        config_path = os.path.join(rubros_dir, entry, "config.json")
        if not os.path.exists(config_path):
            continue

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            print(f"Error loading {config_path}: {e}")
            continue

        key = entry
        welcome_message = config.get("welcome_message", "Hola, ¿en qué te puedo ayudar?")
        quick_actions = config.get("quick_actions", [])

        responses = [
            {
                "id": "welcome",
                "keywords": ["__INIT__", "hola", "inicio", "empezar", "menu"],
                "match_always": False,
                "response": welcome_message,
                "buttons": []
            }
        ]

        # Generate main menu buttons and submenu responses
        for group in quick_actions:
            group_id = group.get("texto", "").lower().replace(" ", "_")[:20]
            group_text = group.get("texto", "Opción")
            group_desc = group.get("description", "")

            # Add button to welcome message
            responses[0]["buttons"].append({
                "label": f"{group.get('emoji', '')} {group_text}".strip(),
                "target": group_id
            })

            # Create response for this group (submenu)
            submenu_buttons = []
            for item in group.get("submenu", []):
                item_id = item.get("texto", "").lower().replace(" ", "_")[:20]
                submenu_buttons.append({
                    "label": f"{item.get('emoji', '')} {item.get('texto')}".strip(),
                    "target": item_id
                })

                # Create leaf response for specific item
                responses.append({
                    "id": item_id,
                    "keywords": [item.get("texto", "").lower()],
                    "response": f"{item.get('description')}\n\n_{item.get('prompt')}_",
                    "include_faq_preview": 1 # Just a flag to maybe show related info
                })

            responses.append({
                "id": group_id,
                "keywords": [group_text.lower()],
                "response": group_desc or f"Opciones de {group_text}",
                "buttons": submenu_buttons
            })

        # Add fallback
        script_data = {
            "key": key,
            "responses": responses,
            "fallback": {
                "response": "No entendí tu consulta. ¿Te gustaría ver el menú principal?",
                "buttons": [
                    {"label": "Ver menú principal", "target": "welcome"}
                ]
            }
        }

        output_path = os.path.join(scripts_dir, f"{key}.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(script_data, f, indent=2, ensure_ascii=False)
        print(f"Generated {output_path}")

if __name__ == "__main__":
    generate_scripts()
