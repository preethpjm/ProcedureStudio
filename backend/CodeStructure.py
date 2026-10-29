


import os

ROOT = r"C:\Users\Preeth\Downloads\procedure-studio".strip()
OUTPUT = "Procedural_codebase_dump.txt"

IGNORE_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".idea",
    ".vscode"
}

CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".java", ".cpp", ".c", ".h", ".hpp",
    ".cs", ".go", ".rs", ".php",
    ".html", ".css", ".scss",
    ".json", ".xml", ".yaml", ".yml",
    ".sql", ".sh", ".bat", ".ps1",
    ".md", ".txt"
}


def build_tree(root):
    lines = []
    print("debug: build_tree called with root:", root)  # Debugging line
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]

        level = os.path.relpath(current, root).count(os.sep)
        indent = "    " * level

        lines.append(f"{indent}{os.path.basename(current)}/")

        for file in sorted(files):
            lines.append(f"{indent}    {file}")

    return "\n".join(lines)


with open(OUTPUT, "w", encoding="utf-8") as out:

    out.write("=" * 80 + "\n")
    out.write("PROJECT STRUCTURE\n")
    out.write("=" * 80 + "\n\n")
    out.write(build_tree(ROOT))
    out.write("\n\n")

    out.write("=" * 80 + "\n")
    out.write("FILE CONTENTS\n")
    out.write("=" * 80 + "\n\n")

    for current, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]

        for file in sorted(files):
            ext = os.path.splitext(file)[1].lower()

            if ext not in CODE_EXTENSIONS:
                continue

            path = os.path.join(current, file)
            rel = os.path.relpath(path, ROOT)

            out.write("\n" + "=" * 80 + "\n")
            out.write(f"FILE: {rel}\n")
            out.write("=" * 80 + "\n\n")

            try:
                with open(path, "r", encoding="utf-8") as f:
                    out.write(f.read())
            except Exception as e:
                out.write(f"<< Could not read file: {e} >>")

            out.write("\n\n")

print(f"\nDone! Output saved as '{OUTPUT}'")
