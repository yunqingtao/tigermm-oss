"""对外检索与安全评估 (core/depot.py) 的测试。

守五组 (网络部分用 stub, 其余全离线):
  A 许可证分档 —— 没许可证 = bad (法律上不能用); copyleft = careful; 宽松 = ok
  B 名称混淆 —— 与知名包近似要抓出来 (抢注/投毒嫌疑)
  C ★ 静态扫描 —— 造一个**假 wheel** 验证: .pth 阻断 · setup.py 阻断 · 风险点识别
  D ★ 裁决 —— 有阻断必 reject · 缺证据不给 safe · 活跃+宽松许可才 safe
  E 已装清单 —— 可追溯 (append event, 不覆盖历史)
★ 关键: 扫描与裁决**绝不执行候选代码** (测试里用纯文本假包即可验证)
"""
import io
import json
import sys
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.depot import (BLOCKERS, POPULAR, days_since, license_class, load_installed,
                        record_installed, render_installed, risk_reach, scan_wheel,
                        typosquat_suspects, verdict)


# ───────────────────── A. 许可证分档 ─────────────────────

class TestLicenseClass:
    @pytest.mark.parametrize("lic", ["MIT", "Apache-2.0", "BSD-3-Clause", "ISC", "Unlicense",
                                     "PSF-2.0", "mit"])
    def test_ok(self, lic):
        assert license_class(lic) == "ok", lic

    @pytest.mark.parametrize("lic", ["GPL-3.0", "AGPL-3.0", "LGPL-2.1", "MPL-2.0"])
    def test_careful(self, lic):
        assert license_class(lic) == "careful", lic

    @pytest.mark.parametrize("lic", ["", None, "NONE", "Proprietary"])
    def test_bad(self, lic):
        assert license_class(lic) == "bad", lic

    def test_full_text_still_detected(self):
        """许可证字段常写整段话 —— 关键词兜底要能认。"""
        assert license_class("This software is released under the MIT License.") == "ok"
        # ★ 实测漏洞: 这些写法靠 \bGPL\b 认不出 (后面紧跟字母/数字), 要单独覆盖
        assert license_class("GNU General Public License v3 (GPLv3)") == "careful"
        assert license_class("GPLv3") == "careful"
        assert license_class("LGPL-2.1") == "careful"
        assert license_class("AGPLv3+") == "careful"

    def test_unknown_license_is_bad_not_ok(self):
        """★ 认不出来就归 bad —— 宁可保守, 不许猜成宽松。"""
        assert license_class("Weird Custom License 1.0") == "bad"


# ───────────────────── B. 名称混淆 ─────────────────────

class TestTyposquat:
    def test_requests_typo(self):
        assert "requests" in typosquat_suspects("requestz")     # 换一个字母
        assert "requests" in typosquat_suspects("request")      # 少一个字母
        assert "requests" in typosquat_suspects("reequests")    # 多一个字母

    def test_separator_insensitive(self):
        assert "pyyaml" in typosquat_suspects("py-yaml") or \
               "pyyaml" in typosquat_suspects("pyaml")

    def test_exact_name_not_suspect(self):
        for p in POPULAR:
            assert typosquat_suspects(p) == [], p

    def test_unrelated_name_clean(self):
        assert typosquat_suspects("ddgs") == []

    def test_distant_name_clean(self):
        assert typosquat_suspects("zzzzzzzzzz") == []


# ───────────────────── C. ★ 静态扫描 (假 wheel, 不执行任何东西) ─────────────────────

def make_wheel(tmp_path: Path, files: dict, name="pkg") -> Path:
    p = tmp_path / f"{name}.whl"
    with zipfile.ZipFile(p, "w") as z:
        for fn, content in files.items():
            z.writestr(fn, content)
    return p


class TestScanWheel:
    def test_clean_package(self, tmp_path):
        w = make_wheel(tmp_path, {"pkg/__init__.py": "def add(a, b):\n    return a + b\n",
                                  "pkg/core.py": "import math\n\n\ndef f(x):\n    return math.sqrt(x)\n"})
        s = scan_wheel(w)
        assert s["ok"] and s["py_files"] == 2
        assert s["blockers"] == [] and s["risks"] == [], s

    def test_pth_file_is_blocker(self, tmp_path):
        """★ .pth 在解释器启动时执行 —— 供应链投毒经典手法, 必须阻断。"""
        w = make_wheel(tmp_path, {"pkg/__init__.py": "x = 1\n",
                                  "evil.pth": "import os; os.system('calc')\n"})
        s = scan_wheel(w)
        assert "pth_file" in s["blockers"], s
        assert "pth_file" in BLOCKERS

    def test_setup_py_in_wheel_is_blocker(self, tmp_path):
        w = make_wheel(tmp_path, {"pkg/__init__.py": "x=1\n", "setup.py": "from setuptools import setup\nsetup()\n"})
        assert "setup_py_in_wheel" in scan_wheel(w)["blockers"]

    def test_subprocess_detected(self, tmp_path):
        w = make_wheel(tmp_path, {"pkg/a.py": "import subprocess\nsubprocess.run(['whoami'])\n"})
        assert "subprocess" in [r["kind"] for r in scan_wheel(w)["risks"]]

    def test_os_system_detected(self, tmp_path):
        w = make_wheel(tmp_path, {"pkg/a.py": "import os\nos.system('dir')\n"})
        assert "subprocess" in [r["kind"] for r in scan_wheel(w)["risks"]]

    def test_base64_exec_detected(self, tmp_path):
        """★ 混淆执行: base64 解码后 exec —— 典型藏载荷写法。"""
        w = make_wheel(tmp_path, {"pkg/a.py":
                                  "import base64\nexec(base64.b64decode('cHJpbnQoMSk='))\n"})
        assert "base64_exec" in [r["kind"] for r in scan_wheel(w)["risks"]]

    def test_credential_paths_detected(self, tmp_path):
        w = make_wheel(tmp_path, {"pkg/a.py": "p = '~/.ssh/id_rsa'\npassword = 'x'\n"})
        kinds = [r["kind"] for r in scan_wheel(w)["risks"]]
        assert "credential_paths" in kinds, kinds

    def test_winreg_and_ctypes(self, tmp_path):
        w = make_wheel(tmp_path, {"pkg/a.py": "import winreg\nimport ctypes\n"})
        kinds = [r["kind"] for r in scan_wheel(w)["risks"]]
        assert "winreg" in kinds and "ctypes" in kinds, kinds

    def test_network_detected(self, tmp_path):
        w = make_wheel(tmp_path, {"pkg/a.py": "import requests\nrequests.post('http://x', data={})\n"})
        assert "network" in [r["kind"] for r in scan_wheel(w)["risks"]]

    def test_comments_ignored(self, tmp_path):
        """注释里的风险词不该误报 (否则每条都红)。"""
        w = make_wheel(tmp_path, {"pkg/a.py": "# 这里不该用 subprocess.run()\nimport math\n"})
        assert scan_wheel(w)["risks"] == []

    def test_never_executes(self, tmp_path):
        """★ 硬不变量: 扫描不得执行包内代码。
        假包里放一个"会写文件"的语句 —— 若被 import/exec, 文件就会出现。"""
        canary = tmp_path / "CANARY_EXECUTED.txt"
        w = make_wheel(tmp_path, {"pkg/__init__.py":
                                  f"open({str(canary)!r}, 'w').write('boom')\n"})
        scan_wheel(w)
        assert not canary.exists(), "扫描执行了候选代码 (严重!)"

    def test_bad_zip_handled(self, tmp_path):
        bad = tmp_path / "bad.whl"
        bad.write_bytes(b"not a zip at all")
        s = scan_wheel(bad)
        assert s["ok"] is False and s["error"], s

    def test_risks_capped_files_listed(self, tmp_path):
        files = {f"pkg/m{i}.py": "import subprocess\nsubprocess.run(['x'])\n" for i in range(9)}
        w = make_wheel(tmp_path, files)
        r = [x for x in scan_wheel(w)["risks"] if x["kind"] == "subprocess"][0]
        assert r["count"] == 9 and len(r["files"]) <= 5, r


# ───────────────────── D. ★ 裁决 ─────────────────────

class TestVerdict:
    def _ev(self, **kw):
        base = {"name": "somepkg", "license": "MIT", "requires_python": ">=3.9",
                "latest_upload": time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                               time.gmtime(time.time() - 86400 * 30)),
                "scanned": True, "risks": [], "blockers": []}
        base.update(kw)
        return base

    def test_clean_active_mit_is_safe(self):
        v = verdict(self._ev())
        assert v["level"] == "safe", v

    def test_no_license_is_reject(self):
        v = verdict(self._ev(license=""))
        assert v["level"] == "reject" and "no_license" in v["blockers"], v

    def test_pth_is_reject(self):
        v = verdict(self._ev(blockers=["pth_file"]))
        assert v["level"] == "reject", v

    def test_not_scanned_not_safe(self):
        """★ 只看元数据不给 safe —— 必须真扫过包内容。"""
        v = verdict(self._ev(scanned=False))
        assert v["level"] != "safe", v

    def test_archived_and_stale_is_not_safe(self):
        old = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 86400 * 1000))
        v = verdict(self._ev(archived=True, latest_upload=old, requires_python=""))
        assert v["level"] != "safe", v

    def test_copyleft_capped_at_caution(self):
        v = verdict(self._ev(license="GPL-3.0"))
        assert v["level"] == "caution", v

    def test_python_too_new_rejected(self):
        v = verdict(self._ev(requires_python=">=3.13"), py_version="3.11")
        assert v["level"] != "safe" and any("3.13" in r for r in v["reasons"]), v

    def test_yanked_rejected(self):
        v = verdict(self._ev(yanked=True))
        assert v["level"] == "reject", v

    def test_typosquat_downgrades(self):
        v = verdict(self._ev(name="requestz"))
        assert v["level"] != "safe", v

    def test_risks_downgrade_but_not_reject(self):
        """风险点由人看 —— 不该自动 reject (但也不能算 safe)。"""
        v = verdict(self._ev(risks=[{"kind": "network", "files": ["a.py"], "count": 1}]))
        assert v["level"] == "caution" and "network" in " ".join(v["reasons"]), v

    def test_pure(self):
        ev = self._ev()
        assert verdict(ev) == verdict(ev)

    def test_blocker_beats_high_score(self):
        v = verdict(self._ev(license="MIT", blockers=["pth_file"],
                            latest_upload=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())))
        assert v["level"] == "reject", v


# ───────────────────── E. 已装清单 ─────────────────────

class TestInstalledLog:
    def test_record_and_load(self, tmp_path):
        record_installed("ddgs", {"kind": "dep", "source": "pypi", "license": "MIT",
                                  "version": "9.16.0", "verdict": "safe"}, data_dir=tmp_path)
        d = load_installed(tmp_path)
        assert "ddgs" in d and d["ddgs"]["license"] == "MIT"

    def test_events_append_not_overwrite(self, tmp_path):
        """★ 可追溯: 同一个东西的多次动作都要留痕 (不许覆盖历史)。"""
        record_installed("x", {"kind": "dep", "verdict": "caution"}, data_dir=tmp_path)
        record_installed("x", {"kind": "dep", "verdict": "safe", "note": "装了"}, data_dir=tmp_path)
        d = load_installed(tmp_path)
        assert len(d["x"]["events"]) == 2, d["x"]
        assert d["x"]["verdict"] == "safe"

    def test_render(self, tmp_path):
        record_installed("ddgs", {"kind": "dep", "source": "pypi", "license": "MIT"},
                         data_dir=tmp_path)
        txt = render_installed(tmp_path)
        assert "ddgs" in txt and "MIT" in txt

    def test_render_empty(self, tmp_path):
        assert "空" in render_installed(tmp_path)

    def test_days_since(self):
        assert days_since("") is None
        assert days_since("garbage") is None
        d = days_since(time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 86400 * 5)))
        assert d is not None and 4.5 < d < 5.5, d

# ───────────────────── F. ★ 风险点可达性 (实测驱动) ─────────────────────

class TestRiskReach:
    """★ 实测来由: ddgs 扫出 subprocess, 一看在 cli.py 的 `serve --detach` 里
    (起它自己的 uvicorn API 服务), 而我们只用 `from ddgs import DDGS` —— 库路径不经过 cli。
    不加区分的话, 每个带 CLI 的包都被记成"有风险", 闸门就成噪音了。"""

    def test_cli_py_is_entry_only(self):
        r = risk_reach([{"kind": "subprocess", "files": ["ddgs/cli.py"], "count": 1}])
        assert len(r["entry_only"]) == 1 and r["lib"] == [], r

    def test_library_path_is_lib(self):
        r = risk_reach([{"kind": "network", "files": ["pkg/http_client.py"], "count": 1}])
        assert len(r["lib"]) == 1 and r["entry_only"] == [], r

    def test_mixed_split(self):
        r = risk_reach([{"kind": "subprocess", "files": ["pkg/core.py", "pkg/cli.py"], "count": 2}])
        assert [x["files"] for x in r["lib"]] == [["pkg/core.py"]]
        assert [x["files"] for x in r["entry_only"]] == [["pkg/cli.py"]]

    @pytest.mark.parametrize("f", ["pkg/__main__.py", "pkg/main.py", "pkg/api_server/api.py",
                                  "tests/test_x.py", "examples/demo.py", "pkg/scripts/x.py"])
    def test_entry_shapes(self, f):
        assert risk_reach([{"kind": "subprocess", "files": [f], "count": 1}])["entry_only"], f

    @pytest.mark.parametrize("f", ["pkg/ddgs.py", "pkg/base.py", "pkg/http_client.py"])
    def test_lib_shapes(self, f):
        assert risk_reach([{"kind": "network", "files": [f], "count": 1}])["lib"], f

    def test_pure(self):
        r = [{"kind": "subprocess", "files": ["a/cli.py"], "count": 1}]
        assert risk_reach(r) == risk_reach(r)

    def test_empty(self):
        assert risk_reach([]) == {"lib": [], "entry_only": []}

    def test_entry_only_risk_is_reviewable_not_safe(self):
        """★ 入口路径风险 → reviewable (人可放行) 但**绝不自动判 safe**。"""
        ev = {"name": "ddgs", "license": "MIT", "requires_python": ">=3.10", "scanned": True,
              "latest_upload": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
              "risks": [{"kind": "subprocess", "files": ["ddgs/cli.py"], "count": 1}],
              "blockers": []}
        v = verdict(ev)
        assert v["level"] == "caution", v
        assert v["reviewable"] is True, v
        assert v["entry_only_risks"] == ["subprocess"] and v["lib_risks"] == []

    def test_lib_risk_not_reviewable(self):
        ev = {"name": "x", "license": "MIT", "requires_python": ">=3.10", "scanned": True,
              "latest_upload": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
              "risks": [{"kind": "network", "files": ["x/http_client.py"], "count": 1}],
              "blockers": []}
        v = verdict(ev)
        assert v["reviewable"] is False and v["level"] == "caution", v
