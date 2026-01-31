import os
import re

versions_dir = 'migrations/versions'
revisions = {}
down_revisions = {}

for filename in os.listdir(versions_dir):
    if not filename.endswith('.py') or filename == '__init__.py':
        continue

    filepath = os.path.join(versions_dir, filename)
    with open(filepath, 'r') as f:
        content = f.read()

        rev_match = re.search(r"revision\s*=\s*['\"]([^'\"]+)['\"]", content)
        down_match = re.search(r"down_revision\s*=\s*['\"]([^'\"]+)['\"]", content)
        down_tuple_match = re.search(r"down_revision\s*=\s*\((.*?)\)", content, re.DOTALL)

        if rev_match:
            rev = rev_match.group(1)
            revisions[rev] = filename

            if down_match:
                down = down_match.group(1)
                down_revisions.setdefault(down, []).append(rev)
            elif down_tuple_match:
                # tuple of revisions
                items = [x.strip().strip("'\"") for x in down_tuple_match.group(1).split(',')]
                for item in items:
                    if item:
                        down_revisions.setdefault(item, []).append(rev)

all_revs = set(revisions.keys())
parents = set(down_revisions.keys())
heads = all_revs - parents

print(f"Total revisions: {len(all_revs)}")
print(f"Heads ({len(heads)}):")
for h in heads:
    print(f"  {h} ({revisions.get(h)})")

print("\nChildren of 20300114:")
print(down_revisions.get('20300114', 'None'))
