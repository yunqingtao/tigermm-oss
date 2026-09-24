"""
Browser automation plugin — Playwright-based headless browser control.
Security: domain whitelist, blocks file:// and localhost.
"""
import asyncio, logging

logger = logging.getLogger("tools.browser")

TOOL = {
    "name": "browser",
    "description": "BACKGROUND/INVISIBLE headless browser (user CANNOT see it). For web scraping, API testing, page analysis. NOT for 'open browser' — use windows_desktop action=open_browser to launch visible Chrome/Edge.",
    "keywords": ["打开网页", "浏览网页", "填表", "browser"],
    "params": [
        {"name": "action", "type": "str", "required": True,
         "enum": ["navigate", "click", "type", "read", "screenshot", "execute", "close", "snapshot"],
         "description": "navigate=open URL, click=click element, type=enter text, read=get page text, screenshot=take picture, execute=run JS, close=shutdown, snapshot=extract structured page (links/buttons/inputs/text for AI reading)"},
        {"name": "url", "type": "str", "required": False,
         "description": "URL for navigate action"},
        {"name": "selector", "type": "str", "required": False,
         "description": "CSS selector for click or type actions"},
        {"name": "text", "type": "str", "required": False,
         "description": "Text to type (for 'type' action) or JavaScript code (for 'execute' action)"},
    ]
}

# ═══ Security ═══
BLOCKED_SCHEMES = {"file:", "chrome:", "about:", "javascript:", "data:"}
BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
BLOCKED_SUBNETS = ("10.", "172.16.", "192.168.")

ALLOWED_DOMAINS = []  # Empty = allow all (safe by default, can be restricted)

_browser = None
_page = None


def _check_url(url: str) -> tuple[bool, str]:
    """Security check. Returns (allowed, reason)."""
    url_lower = url.lower()
    for scheme in BLOCKED_SCHEMES:
        if url_lower.startswith(scheme):
            return False, f"Blocked scheme: {scheme}"
    if not url_lower.startswith(("http://", "https://")):
        return False, "Only http/https allowed"
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in BLOCKED_HOSTS:
        return False, f"Blocked host: {host}"
    if any(host.startswith(net) for net in BLOCKED_SUBNETS):
        return False, f"Blocked private network: {host}"
    if ALLOWED_DOMAINS and host not in ALLOWED_DOMAINS and not any(host.endswith("." + d) for d in ALLOWED_DOMAINS):
        return False, f"Domain not in whitelist: {host}"
    return True, ""


async def _get_browser():
    global _browser, _page
    if _browser is None:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return None, "Playwright not installed. Run: pip install playwright && playwright install chromium"
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(
            headless=True,
            proxy={"server": "http://127.0.0.1:7890"}
        )
        _page = await _browser.new_page()
        logger.info("Browser: started (headless chromium)")
    return _browser, None


async def execute(**kwargs) -> dict:
    action = kwargs.get("action", "navigate")
    url = kwargs.get("url", "")
    selector = kwargs.get("selector", "")
    text = kwargs.get("text", "")

    if action == "close":
        global _browser, _page
        if _browser:
            await _browser.close()
            _browser = None; _page = None
        return {"success": True, "output": "Browser closed"}

    if action == "navigate":
        ok, reason = _check_url(url)
        if not ok:
            return {"success": False, "output": f"浏览器: {reason}", "error": reason}
        browser, err = await _get_browser()
        if err:
            return {"success": False, "output": f"浏览器启动失败: {err}", "error": err}
        try:
            await _page.goto(url, timeout=15000)
            title = await _page.title()
            return {"success": True, "output": f"Loaded: {title}\nURL: {url}"}
        except Exception as e:
            return {"success": False, "output": f"页面加载失败: {e}", "error": str(e)[:200]}

    if action == "read":
        if not _page:
            return {"success": False, "output": "未加载页面，先调用navigate", "error": "No page loaded. Use navigate first."}
        try:
            text = await _page.inner_text("body")
            return {"success": True, "output": text[:8000]}
        except Exception as e:
            return {"success": False, "output": f"操作失败: {e}", "error": str(e)[:200]}

    if action == "click":
        if not _page:
            return {"success": False, "output": "未加载页面，先调用navigate", "error": "No page loaded. Use navigate first."}
        try:
            await _page.click(selector, timeout=5000)
            return {"success": True, "output": f"Clicked: {selector}"}
        except Exception as e:
            return {"success": False, "output": f"操作失败: {e}", "error": str(e)[:200]}

    if action == "type":
        if not _page:
            return {"success": False, "output": "未加载页面", "error": "No page loaded."}
        try:
            await _page.fill(selector, text, timeout=5000)
            return {"success": True, "output": f"Typed {len(text)} chars into {selector}"}
        except Exception as e:
            return {"success": False, "output": f"操作失败: {e}", "error": str(e)[:200]}

    if action == "screenshot":
        if not _page:
            return {"success": False, "output": "未加载页面", "error": "No page loaded."}
        try:
            import tempfile, os
            path = os.path.join(tempfile.gettempdir(), f"tmm_screenshot_{id(kwargs)}.png")
            await _page.screenshot(path=path, full_page=False)
            return {"success": True, "output": f"Screenshot saved: {path}"}
        except Exception as e:
            return {"success": False, "output": f"操作失败: {e}", "error": str(e)[:200]}

    if action == "execute":
        if not _page:
            return {"success": False, "output": "未加载页面", "error": "No page loaded."}
        try:
            result = await _page.evaluate(text)
            return {"success": True, "output": str(result)[:5000]}
        except Exception as e:
            return {"success": False, "output": f"操作失败: {e}", "error": str(e)[:200]}

    if action == "snapshot":
        """Extract structured page content — links, buttons, inputs, text — AI-readable."""
        if not _page:
            return {"success": False, "output": "未加载页面，先调用navigate", "error": "No page loaded."}
        try:
            struct = await _page.evaluate("""() => {
                const out = [];
                // Title
                out.push('TITLE: ' + document.title);
                // Links (max 30)
                out.push('\\nLINKS:');
                const links = document.querySelectorAll('a[href]');
                let n = 0;
                for (const a of links) {
                    const txt = (a.textContent || '').trim().slice(0, 80);
                    const href = a.href.slice(0, 120);
                    if (txt) { out.push(`  [${n}] ${txt} -> ${href}`); n++; }
                    if (n >= 30) break;
                }
                // Buttons
                out.push('\\nBUTTONS:');
                const btns = document.querySelectorAll('button, input[type=submit], input[type=button]');
                n = 0;
                for (const b of btns) {
                    const txt = (b.value || b.textContent || b.name || '').trim().slice(0, 60);
                    if (txt) { out.push(`  [${n}] ${txt}`); n++; }
                    if (n >= 15) break;
                }
                // Inputs
                out.push('\\nINPUTS:');
                const inputs = document.querySelectorAll('input:not([type=submit]):not([type=button]):not([type=hidden]), textarea, select');
                n = 0;
                for (const inp of inputs) {
                    const name = inp.name || inp.id || '';
                    const type = inp.type || 'text';
                    const placeholder = inp.placeholder || '';
                    const label = (name + ' ' + placeholder).trim().slice(0, 60);
                    if (label) { out.push(`  [${n}] ${type}: ${label}`); n++; }
                    if (n >= 15) break;
                }
                // Main text (first 3000 chars of body)
                out.push('\\nTEXT:');
                const body = (document.body?.innerText || '').slice(0, 3000);
                out.push(body);
                return out.join('\\n');
            }""")
            return {"success": True, "output": struct[:8000]}
        except Exception as e:
            return {"success": False, "output": f"操作失败: {e}", "error": str(e)[:200]}

    return {"success": False, "output": f"未知操作: {action}", "error": f"Unknown action: {action}"}


# Alias for PluginManager
async def run(**kwargs):
    return await execute(**kwargs)
