"""Purge ALL non-ASCII bytes from eval py files."""
import os

FILES = [
    "backend/eval/run_eval.py",
    "backend/eval/clean_features.py",
    "backend/eval/scenario_generator.py",
]

for fpath in FILES:
    if not os.path.exists(fpath):
        continue
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    # Replace any non-ASCII char with closest ASCII equivalent or remove
    clean = ""
    for ch in content:
        if ord(ch) > 127:
            # Common replacements
            if ch in "\u2014\u2013":
                clean += "--"
            elif ch in "\u2018\u2019":
                clean += "'"
            elif ch in "\u201c\u201d":
                clean += '"'
            elif ch == "\u2192":
                clean += "->"
            elif ch == "\u2265":
                clean += ">="
            elif ch == "\u2264":
                clean += "<="
            else:
                clean += ""  # drop emoji and other special chars
        else:
            clean += ch
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(clean)
    dropped = len(content) - len(clean)
    print(f"Fixed: {fpath} (dropped {dropped} non-ASCII chars)")

print("Done")
