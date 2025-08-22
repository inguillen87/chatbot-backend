import json

def populate_contactos():
    # Load the source files
    with open('data/municipios/default/contactos_especializados.json', 'r', encoding='utf-8') as f:
        contactos_especializados = json.load(f)

    with open('data/municipios/default/tramites.json', 'r', encoding='utf-8') as f:
        tramites = json.load(f)

    # Create the new data structure
    contactos_utiles = {
        "categorias": [
            {
                "nombre_categoria": "Emergencias",
                "contactos": [
                    {
                        "nombre": "Policía",
                        "descripcion": "Para emergencias policiales.",
                        "telefono": "911",
                        "url": ""
                    },
                    {
                        "nombre": "Bomberos",
                        "descripcion": "Para incendios y rescates.",
                        "telefono": "100",
                        "url": ""
                    },
                    {
                        "nombre": "Defensa Civil",
                        "descripcion": "Para asistencia en desastres naturales.",
                        "telefono": "103",
                        "url": ""
                    }
                ]
            },
            {
                "nombre_categoria": "Servicios Municipales",
                "contactos": []
            },
            {
                "nombre_categoria": "Trámites Frecuentes",
                "contactos": []
            }
        ]
    }

    # Populate "Servicios Municipales"
    for key, value in contactos_especializados.items():
        contactos_utiles["categorias"][1]["contactos"].append({
            "nombre": key,
            "descripcion": value.get("titulo", ""),
            "telefono": value.get("telefono", ""),
            "url": ""
        })

    # Populate "Trámites Frecuentes"
    for key, value in tramites.items():
        if key not in ["default", "Otros"]:
            # Check if the "botones" list exists and is not empty
            url = ""
            if "botones" in value and value["botones"]:
                url = value["botones"][0].get("url", "")

            contactos_utiles["categorias"][2]["contactos"].append({
                "nombre": key,
                "descripcion": value.get("titulo", ""),
                "telefono": value.get("telefono", ""),
                "url": url
            })

    # Write the new data to the file
    with open('data/municipios/default/contactos_utiles.json', 'w', encoding='utf-8') as f:
        json.dump(contactos_utiles, f, indent=2, ensure_ascii=False)

if __name__ == '__main__':
    populate_contactos()
    print("contactos_utiles.json has been populated successfully.")
