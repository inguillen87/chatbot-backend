import sys

file_path = 'routes/document_intelligence.py'

with open(file_path, 'r') as f:
    content = f.read()

# Locate the loop in document_intelligence_commit
target = r'        precio = item.get("precio") or item.get("price")'
replacement = r'        precio = parse_price(item.get("precio") or item.get("price"))'

if target in content:
    content = content.replace(target, replacement)
    print("Updated price fetching to use parse_price")
else:
    print("Could not find target price line")
    sys.exit(1)

# Locate the instantiation of CatalogoItem to ensure we store it as string if model expects string,
# or if we want to store the parsed float.
# The original code:
# precio=str(item.get("precio") or ""),

# But we updated the variable `precio` to be a float.
# So `precio=str(precio)` is correct if the column is string.
# But `item.get("precio")` in the original code referred to the raw item dict.
# In my loop:
# normalized_items.append({ ... "precio": precio ... })

# The `normalized_items` list uses the parsed `precio`.
# Then in the second loop:
# for idx, item in enumerate(normalized_items):
#    catalog_item = CatalogoItem( ... precio=str(item.get("precio") or "") ...)

# Since `item["precio"]` is now a float (from parse_price), `str(float)` gives "1500.0".
# This is fine.

# However, I should check if there are other places.

with open(file_path, 'w') as f:
    f.write(content)
