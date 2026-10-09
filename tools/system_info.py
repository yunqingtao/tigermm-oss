"""
System info plugin - OS, disk, processes, network.
"""
import platform, os, subprocess, shutil

PLUGIN = {
    "name": "system_info",
    "description": "System info: sysinfo, disk, ps, network",
    "version": "1.2",
    "requires": [],
    "trigger": ["system", "sysinfo", "disk", "ps", "cpu"],
    "permission": ["safe"],
    "category": "data",
}

TOOL = {
    "name": "system_info",
    "description": "PREFERRED for disk/time queries. Use instead of shell_exec for disk/space/time checks.",
    # 🔴 2026-08-24: 去掉 "ps" 和 "time" — "ps" 是 "https" 的子串,
    # 任何含 URL 的消息都被 Plugin 关键词路由劫持成 system_info(返回系统横幅);
    # "time" 会误匹配英文文本(sometimes/lifetime)。保留中文关键词 + disk/sysinfo。
    "keywords": ["disk", "sysinfo", "时间", "日期"],
    "params": [
        {"name": "action", "type": "str", "required": False,
         "description": "Use disk for drive space (total/used/free GB), time for current date/time, sysinfo for OS info"},
        {"name": "drive", "type": "str", "required": False,
         "description": "Specific drive letter, e.g. D:"}
    ]
}

async def run(**kwargs):
    action = kwargs.get("action", "sysinfo")
    if action == "time":
        import datetime
        now = datetime.datetime.now()
        return {"success": True,
                "output": now.strftime("%Y-%m-%d %H:%M:%S") + " 星期" + "一二三四五六日"[now.weekday()]}
    if action == "sysinfo":
        info = "OS: " + platform.system() + " " + platform.release() + "\n"
        info += "Host: " + platform.node() + "\n"
        info += "Python: " + platform.python_version() + "\n"
        info += "CWD: " + os.getcwd()
        return {"success": True, "output": info}
    elif action == "disk":
        lines = []
        for p in ["C:/", "D:/", "E:/"]:
            try:
                u = shutil.disk_usage(p)
                gb = 1024**3
                lines.append("  " + p + " " + str(round(u.used/gb,1)) + "/" + str(round(u.total/gb,1)) + "GB (" + str(round(u.free/gb,1)) + " free)")
            except Exception:
                pass
        return {"success": True, "output": "\n".join(lines) or "No drives"}
    elif action == "ps":
        try:
            r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10, encoding='gbk', errors='replace')
            out_lines = []
            for ln in r.stdout.strip().split("\n")[:20]:
                if "," in ln:
                    parts = ln.split(",")
                    name = parts[0].strip('"')
                    pid = parts[1].strip('"')
                    mem = parts[-1].strip('"') if len(parts) >= 5 else ""
                    out_lines.append(f"  {name[:30]:30s} PID:{pid} {mem}")
            return {"success": True, "output": "\n".join(out_lines) if out_lines else "No processes"}
        except Exception as e:
            return {"success": False, "error": f"Process list failed: {e}"}
    return {"success": False, "error": "Unknown action: " + action}
