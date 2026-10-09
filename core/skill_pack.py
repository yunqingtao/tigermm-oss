"""TMM Skill Packager -- pack/unpack distributable skills."""
import json, os, shutil, zipfile
from pathlib import Path

OPTIONAL_DIRS = ["scripts", "references", "templates", "assets"]

def create_skill(name, description, author="", version="1.0.0", output_dir=None):
    d = Path(output_dir or ".") / name
    d.mkdir(parents=True, exist_ok=True)
    meta = {"name": name, "id": name.lower().replace(" ","_"), "version": version,
            "author": author, "description": description, "tags": [],
            "max_exec_time": 300, "arguments": [],
            "permissions": {"file_read": False, "file_write": False, "shell_exec": False, "network": False}}
    with open(d / "skill.json", "w", encoding="utf-8") as f: json.dump(meta, f, indent=2, ensure_ascii=False)
    (d / "README.md").write_text("# " + name + "\n\n" + description + "\n", encoding="utf-8")
    (d / "workflow.md").write_text("# " + name + "\n\n## Steps\n1. \n2. \n3. \n\n## Rules\n1. \n", encoding="utf-8")
    for sub in OPTIONAL_DIRS: (d / sub).mkdir(exist_ok=True)
    return str(d)

def pack_skill(skill_dir, output_path=None):
    skill_dir = Path(skill_dir)
    if not (skill_dir / "skill.json").exists(): raise FileNotFoundError("skill.json required")
    meta = json.loads((skill_dir / "skill.json").read_text(encoding="utf-8"))
    name = meta.get("id", skill_dir.name)
    output = Path(output_path) if output_path else Path(name + ".tsk")
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(skill_dir):
            for f in files:
                fp = Path(root) / f
                zf.write(fp, fp.relative_to(skill_dir))
    return str(output)

def unpack_skill(package_path, install_dir=None):
    pkg = Path(package_path)
    if pkg.suffix != ".tsk": raise ValueError("Expected .tsk file")
    dest = Path(install_dir) if install_dir else Path("skills")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(pkg) as zf:
        meta = json.loads(zf.read("skill.json").decode("utf-8"))
        target = dest / meta.get("id", pkg.stem)
        if target.exists(): shutil.rmtree(target)
        zf.extractall(target)
    return str(target)

def list_installed(skills_dir="skills"):
    sd = Path(skills_dir)
    if not sd.exists(): return []
    results = []
    for d in sorted(sd.iterdir()):
        if d.is_dir() and (d / "skill.json").exists():
            meta = json.loads((d / "skill.json").read_text(encoding="utf-8"))
            results.append({"id": meta.get("id",""), "name": meta.get("name",""),
                            "version": meta.get("version","1.0"),
                            "description": meta.get("description",""),
                            "path": str(d)})
    return results