"""Import original DSH Pet GIFs without executing any upstream code.

Usage: python scripts/import_dsh_assets.py <path-to-dsh-pet-checkout>
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


def main(source: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    target = root / "assets/skins/dsh-maid"
    target.mkdir(parents=True, exist_ok=True)
    tree = ast.parse((source / "scripts/encode_preview_gifs.py").read_text(encoding="utf-8"))
    names = next(
        ast.literal_eval(node.value) for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(item, ast.Name) and item.id == "PINYIN" for item in node.targets)
    )
    # Remove JSONC comments, but never match comment markers inside JSON strings.
    raw = (source / "dsh-pet/assets/config.jsonc").read_text(encoding="utf-8")
    raw = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/',
                 lambda match: match[0] if match[0].startswith('"') else "", raw)
    animations = json.loads(raw)["animations"]
    groups = [{"name": "互动与移动", "actions": (
        animations["idle"] + animations["turn"] + animations["drag"]
        + animations["clicks"] + [item["name"] for item in animations["moves"]["actions"]]
    )}]
    groups += [{"name": item["id"], "actions": item["actions"]}
               for item in animations["categories"]]
    for label, key in (("工作状态", "workStatus"), ("碎碎念", "whisper"), ("钱袋表情", "balance")):
        groups.append({"name": label, "actions": animations["events"][key]})
    assert set(names) == {name for group in groups for name in group["actions"]}
    files, hashes = {}, {}
    for name, stem in names.items():
        filename = stem + ".gif"
        source_file = source / "dsh-pet/assets/preview" / filename
        shutil.copyfile(source_file, target / filename)
        files[name] = filename
        hashes[filename] = hashlib.sha256(source_file.read_bytes()).hexdigest()
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    manifest = {
        "source": "https://github.com/PC2005-cloud/dsh-pet",
        "commit": commit, "license": "MIT", "files": files, "groups": groups,
        "idle": animations["idle"][0], "clicks": animations["clicks"],
        "drag": animations["drag"][0],
        "idle_pool": [name for item in animations["categories"][:3] for name in item["actions"]],
        "sha256": hashes,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(source / "LICENSE", root / "assets/LICENSE-DSHPet.txt")
    print(f"Imported {len(files)} original GIFs from {commit}")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
