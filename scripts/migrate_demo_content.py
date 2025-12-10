
import json
import os
import shutil

def migrate_demos():
    source_file = "data/demo_rubros.json"
    base_dir = "data/pyme/rubros"

    with open(source_file, "r", encoding="utf-8") as f:
        demos = json.load(f)

    # Mapping keys to directory names if they differ, or just use the key
    # Based on the directories I created:
    # almacen, ferreteria, local_comercial_general, medico_general, energia, inmobiliaria, fintech, seguros, logistica
    # Bodega already exists.

    key_map = {
        "almacen": "almacen",
        "bodega": "bodega",
        "ferreteria": "ferreteria",
        "local_comercial_general": "local_comercial_general",
        "medico_general": "medico_general",
        "municipio": None, # Skip municipality, it goes to data/municipios
        "energia": "energia",
        "inmobiliaria": "inmobiliaria",
        "fintech": "fintech",
        "seguros": "seguros",
        "logistica": "logistica"
    }

    for demo in demos:
        key = demo.get("key")
        target_dir_name = key_map.get(key)

        if not target_dir_name:
            print(f"Skipping {key}...")
            continue

        target_path = os.path.join(base_dir, target_dir_name)
        os.makedirs(target_path, exist_ok=True)

        print(f"Migrating {key} to {target_path}...")

        # 1. config.json
        config_data = {
            "nombre": demo.get("nombre"),
            "descripcion": demo.get("descripcion"),
            "prompt_context": demo.get("prompt_context"),
            "welcome_message": demo.get("welcome_message"),
            "resources": demo.get("resources", []),
            "quick_actions": demo.get("quick_actions", []),
            "capabilities": demo.get("capabilities", []),
            "keywords": demo.get("keywords", [])
        }

        with open(os.path.join(target_path, "config.json"), "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)

        # 2. faq.json (Synthesized from quick_actions for initial population)
        faqs = []
        for group in demo.get("quick_actions", []):
            for item in group.get("submenu", []):
                faqs.append({
                    "question": item.get("texto"),
                    "answer": item.get("description"), # Using description as a short answer placeholder
                    "extended_answer": item.get("prompt") # The 'prompt' was what the user would say, but acts as context
                })

        with open(os.path.join(target_path, "faq.json"), "w", encoding="utf-8") as f:
            json.dump(faqs, f, indent=2, ensure_ascii=False)

        # 3. menu.json (Optional, if we want a separate menu structure)
        # For now, quick_actions in config.json is enough, but let's keep it clean

    print("Migration complete.")

if __name__ == "__main__":
    migrate_demos()
