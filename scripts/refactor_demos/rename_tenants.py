import json
import os
import re

def rename_in_file(filepath, mapping):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        new_content = content
        for old, new in mapping.items():
            new_content = new_content.replace(old, new)

        if new_content != content:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"Updated {filepath}")
    except Exception as e:
        print(f"Error processing {filepath}: {e}")

def main():
    mapping = {
        "Bodega Cuatro Fincas": "Bodega Demo",
        "ByM Almacén Inteligente": "Almacén Demo",
        "ProObra Ferretería Técnica": "Ferretería Demo",
        "Showroom Moda Urbana": "Tienda de Ropa Demo",
        "Clínica Horizonte": "Clínica Demo",
        "Operador Energético Andino": "Energía Demo",
        "Portfolio Prime Realty": "Inmobiliaria Demo",
        "Aurora Fintech Hub": "Fintech Demo",
        "ShieldOne Seguros Corporativos": "Seguros Demo",
        "Axis Logistics 3PL": "Logística Demo"
    }

    # Files to process
    target_files = [
        "data/demo_rubros.json",
        "init_tenants.py"
    ]

    # Directories to scan
    scan_dirs = [
        "data/pyme/rubros",
        "data/demo_scripts"
    ]

    for filepath in target_files:
        if os.path.exists(filepath):
            rename_in_file(filepath, mapping)

    for directory in scan_dirs:
        for root, dirs, files in os.walk(directory):
            for file in files:
                if file.endswith(".json"):
                    rename_in_file(os.path.join(root, file), mapping)

if __name__ == "__main__":
    main()
