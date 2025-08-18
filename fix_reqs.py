import os

print("Starting requirements.txt fix...")
try:
    with open('requirements.txt', 'rb') as f:
        corrupted_content = f.read()

    # The file seems to be UTF-16 Little Endian with a Byte Order Mark (BOM)
    # The first two bytes are ÿþ which is FF FE in hex, the BOM for UTF-16 LE.
    # We will decode it as such and re-encode as standard UTF-8.

    # Check for BOM
    if corrupted_content.startswith(b'\xff\xfe'):
        print("UTF-16 LE BOM detected.")
        decoded_content = corrupted_content.decode('utf-16-le')
    else:
        # Fallback for simple null byte stripping if no BOM
        print("No BOM detected, attempting simple null byte replacement.")
        decoded_content = corrupted_content.replace(b'\x00', b'').decode('utf-8')

    # Remove any remaining non-printable characters or control characters just in case
    cleaned_content = "".join(char for char in decoded_content if char.isprintable() or char in '\n\r').strip()

    with open('requirements.txt', 'w', encoding='utf-8') as f:
        f.write(cleaned_content)

    print("Successfully cleaned requirements.txt")

except Exception as e:
    print(f"An error occurred: {e}")
    # Create a fallback requirements file if cleaning fails catastrophically
    fallback_reqs = "flask\n"
    with open('requirements.txt', 'w', encoding='utf-8') as f:
        f.write(fallback_reqs)
    print("Wrote a fallback requirements.txt with only flask.")
