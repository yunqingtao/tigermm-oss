"""
Web content scraper — trafilatura + readability, outputs clean markdown.
No API key needed. Local extraction.
"""
import urllib.request, urllib.error
import trafilatura
import re

TOOL = {
    "name": "firecrawl",
    "version": "1.0",
    "description": "Extract web content as clean markdown. Use for scraping articles, docs, any web page. PREFERRED over browser for reading page content.",
    "requires": ["trafilatura"],
    "permission": ["network"],
    "category": "automation",
    "keywords": ["firecrawl", "抓取网页", "爬取网页"],
    "params": [
        {"name": "url", "type": "str", "required": True, "description": "The URL to extract content from (e.g., https://example.com/article)"},
        {"name": "max_chars", "type": "int", "required": False, "description": "Max characters to return (default 8000)"},
    ],
}

PLUGIN = TOOL


def _fetch_html(url: str, timeout: int = 15) -> tuple:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=timeout)
        html = resp.read()
        ct = resp.headers.get("Content-Type", "")
        return html, ct, None
    except urllib.error.HTTPError as e:
        return None, "", f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, "", f"无法连接: {e.reason}"
    except Exception as e:
        return None, "", str(e)


def _html_to_markdown(html_bytes, url: str) -> str:
    try:
        html_str = html_bytes.decode("utf-8", errors="replace")
    except Exception:
        html_str = html_bytes.decode("latin-1", errors="replace")
    result = trafilatura.extract(
        html_str,
        include_comments=False,
        include_tables=True,
        include_images=False,
        include_links=True,
        output_format="markdown",
        url=url,
    )
    if result:
        return result.strip()
    return "(无法提取正文内容)"


async def execute(url: str = "", **kwargs):
    if not url:
        return {"success": False, "output": "抓取失败", "error": "缺少 url 参数。例如: firecrawl(url=https://example.com)"}
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    html, ct, error = _fetch_html(url)
    if error:
        return {"success": False, "output": "抓取失败", "error": f"抓取失败: {error}"}
    md = _html_to_markdown(html, url)
    max_chars = kwargs.get("max_chars", 8000)
    if isinstance(max_chars, str):
        try:
            max_chars = int(max_chars)
        except Exception:
            max_chars = 8000
    if len(md) > max_chars:
        md = md[:max_chars] + f"\n\n...(truncated, {len(md)} chars total)"
    return {
        "success": True,
        "result": md,
        "url": url,
        "length": len(md),
        "format": "markdown",
    }

run = execute
