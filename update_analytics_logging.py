import re

with open('/app/routes/analytics_routes.py', 'r') as f:
    content = f.read()

# Replace print with logger
content = content.replace('print(f"Analytics Error: {e}")', 'current_app.logger.error(f"Analytics Error: {e}", exc_info=True)')

# Add logger to other except blocks that return 500
# Pattern: except Exception as e:\n        return jsonify({"error": str(e)}), 500
# Replacement: except Exception as e:\n        current_app.logger.error(f"Analytics Error: {e}", exc_info=True)\n        return jsonify({"error": str(e)}), 500

pattern = r'except Exception as e:\s+return jsonify\({"error": str\(e\)}\), 500'
replacement = 'except Exception as e:\n        current_app.logger.error(f"Analytics Error: {e}", exc_info=True)\n        return jsonify({"error": str(e)}), 500'

content = re.sub(pattern, replacement, content)

with open('/app/routes/analytics_routes.py', 'w') as f:
    f.write(content)

print("Updated analytics_routes.py")
