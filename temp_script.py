import os

# Define the paths
dir_to_rename = "data/municipios/1"
backup_dir_name = "data/municipios/1_backup"
link_target = "default"
link_name = "data/municipios/1"
base_dir = "data/municipios"

# Ensure the base directory exists
os.makedirs(base_dir, exist_ok=True)

# If the directory exists, rename it
if os.path.isdir(dir_to_rename) and not os.path.islink(dir_to_rename):
    print(f"Directory '{dir_to_rename}' exists, renaming it to '{backup_dir_name}'.")
    try:
        os.rename(dir_to_rename, backup_dir_name)
        print(f"Successfully renamed directory to: {backup_dir_name}")
    except OSError as e:
        print(f"Error renaming directory {dir_to_rename}: {e}")
        exit(1)

# Create the symbolic link if it doesn't exist
if not os.path.exists(link_name):
    try:
        os.symlink(link_target, link_name)
        print(f"Successfully created symbolic link from '{link_name}' to '{link_target}'.")
    except OSError as e:
        print(f"Error creating symbolic link: {e}")
        exit(1)
else:
    print(f"Path '{link_name}' already exists, skipping symlink creation.")
