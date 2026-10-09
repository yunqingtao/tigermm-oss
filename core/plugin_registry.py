"""
PluginRegistry — versioned plugin metadata, dependency tracking, install/remove.
"""
import json, logging, subprocess, sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("core.plugin_registry")

REGISTRY_FILE = None  # set in __init__


class PluginRegistry:
    def __init__(self, data_dir: Path, tools_dir: Path):
        global REGISTRY_FILE
        REGISTRY_FILE = data_dir / "plugin_registry.json"
        self.data_dir = data_dir
        self.tools_dir = tools_dir
        self._plugins: dict = {}
        self._load()

    def _load(self):
        if REGISTRY_FILE.exists():
            try:
                self._plugins = json.loads(REGISTRY_FILE.read_text(encoding='utf-8'))
            except Exception:
                self._plugins = {}
        # Auto-discover installed plugins
        self._scan()

    def _save(self):
        REGISTRY_FILE.write_text(json.dumps(self._plugins, ensure_ascii=False, indent=2), encoding='utf-8')

    def _scan(self):
        """Discover plugins in tools/ and register missing ones."""
        if not self.tools_dir.exists():
            return
        for f in sorted(self.tools_dir.glob("*.py")):
            if f.name.startswith("_"):
                continue
            name = f.stem
            if name not in self._plugins:
                deps = self._read_deps(f)
                self._plugins[name] = {
                    "version": "1.0",
                    "installed": True,
                    "depends": deps,
                    "builtin": True,
                    "path": str(f),
                }
        self._save()

    def _read_deps(self, py_file: Path) -> list:
        """Extract dependencies from plugin's DEPENDS list or TOOL.requires."""
        try:
            code = py_file.read_text(encoding='utf-8')
            # Look for DEPENDS = ["pkg1", "pkg2"]
            import re
            m = re.search(r'DEPENDS\s*=\s*\[(.*?)\]', code, re.DOTALL)
            if m:
                deps_str = m.group(1)
                return [d.strip().strip("'\"") for d in deps_str.split(",") if d.strip()]
            # Look for TOOL requires
            m2 = re.search(r'"requires"\s*:\s*\[(.*?)\]', code, re.DOTALL)
            if m2:
                deps_str = m2.group(1)
                return [d.strip().strip("'\"") for d in deps_str.split(",") if d.strip()]
        except Exception:
            pass
        return []

    def list_plugins(self) -> list[dict]:
        return [
            {"name": n, "version": p.get("version", "?"), "installed": p.get("installed", False),
             "depends": p.get("depends", []), "builtin": p.get("builtin", False)}
            for n, p in sorted(self._plugins.items())
        ]

    def install(self, name: str, pip_package: str = None) -> tuple[bool, str]:
        """Install a plugin by pip package name."""
        pkg = pip_package or name
        if name in self._plugins and self._plugins[name].get("installed"):
            return True, f"Already installed: {name}"

        # pip install
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", pkg],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                return False, f"pip failed: {result.stderr[:200]}"
        except Exception as e:
            return False, str(e)

        self._plugins[name] = {
            "version": "1.0", "installed": True, "depends": [],
            "builtin": False, "path": str(self.tools_dir / f"{name}.py") if (self.tools_dir / f"{name}.py").exists() else ""
        }
        self._save()
        return True, f"Installed: {name}"

    def remove(self, name: str) -> tuple[bool, str]:
        if name not in self._plugins:
            return False, f"Not found: {name}"
        if self._plugins[name].get("builtin"):
            return False, f"Cannot remove builtin plugin: {name}"
        # Check if other plugins depend on this
        dependents = [n for n, p in self._plugins.items() if name in p.get("depends", [])]
        if dependents:
            return False, f"Cannot remove: depended on by {', '.join(dependents)}"
        del self._plugins[name]
        # Remove file if it exists
        py_file = self.tools_dir / f"{name}.py"
        if py_file.exists():
            py_file.unlink()
        self._save()
        return True, f"Removed: {name}"

    def check(self) -> list[dict]:
        """Check all plugins for dependency and import issues."""
        issues = []
        for name, info in self._plugins.items():
            py_file = self.tools_dir / f"{name}.py"
            if not py_file.exists():
                issues.append({"plugin": name, "issue": "file_missing", "path": str(py_file)})
                continue
            # Check imports
            for dep in info.get("depends", []):
                try:
                    __import__(dep)
                except ImportError:
                    issues.append({"plugin": name, "issue": f"import_missing: {dep}"})
        return issues


_registry: Optional[PluginRegistry] = None

def get_registry() -> PluginRegistry:
    global _registry
    if _registry is None:
        from config.settings import DATA_DIR, TOOLS_DIR
        _registry = PluginRegistry(DATA_DIR, TOOLS_DIR)
    return _registry
