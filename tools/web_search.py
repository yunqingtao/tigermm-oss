"""
Web search plugin — provider-based: wttr.in weather + DDG search.
"""
import urllib.request, urllib.parse, re

TOOL = {
    "name": "web_search",
    "version": "2.0",
    "description": "联网搜索网页内容 (也支持天气)。想知道某个词/事件的最新信息时用这个。",
    "requires": [],
    "permission": ["network"],
    "category": "automation",
    # ★★ 2026-09-24 补 (真 bug, 实测): 本工具原来**没有 params/keywords** ——
    #   而 core/tool_schema.get_all_schemas 的收录条件是
    #   `if meta.get("name") and meta.get("params")` ⇒ 缺 params 直接被跳过。
    #   后果: web_search 在 plugin_mgr / 路由层都好使, 但**不在给模型的工具表**里,
    #   于是模型如实回答"我当前可用的工具里没有通用 web 搜索"(用户实测原话),
    #   路由没接管时 (如 "搜索一下 X" 走模型兜底) 就搜不了。
    #   同类缺 params 的还有 17 个工具 (见 tmp/_probe_ws_schema.py 的体检输出)。
    # ★★ 注意: **不要**为它加 "keywords" —— plugin.keyword 路由(优先级 500)
    #   会读 keywords 抢在 IR chain(300) 前匹配, 实测把「打开百度」(本该 open_browser)
    #   劫持成 web_search, 触发 verify_routing_matrix 回归 (3 句胜出者变化)。
    #   模型可见性只靠 name + params 就够 (get_all_schemas 的条件是 name and params)。
    "params": [
        {"name": "query", "type": "str", "required": True,
         "description": "要搜索的内容; 查天气就写城市名, 如 '济南天气'"},
    ],
    "providers": {
        "default": "_search_ddg",
        "routes": {
            "weather": "_search_wttr",
            "general": "_search_ddg",
        },
        "note": "DDG via ddgs library + mihomo:7890, Bing cn fallback"
    }
}

# Legacy PLUGIN dict for backward compat
PLUGIN = TOOL

# City mapping for wttr.in
CITY_MAP = {
    "深圳": "Shenzhen", "北京": "Beijing", "上海": "Shanghai", "广州": "Guangzhou",
    "杭州": "Hangzhou", "成都": "Chengdu", "武汉": "Wuhan", "南京": "Nanjing",
    "西安": "Xian", "重庆": "Chongqing", "天津": "Tianjin", "苏州": "Suzhou",
    "长沙": "Changsha", "郑州": "Zhengzhou", "济南": "Jinan", "青岛": "Qingdao",
    "大连": "Dalian", "厦门": "Xiamen", "福州": "Fuzhou", "合肥": "Hefei",
    "沈阳": "Shenyang", "哈尔滨": "Harbin", "昆明": "Kunming", "贵阳": "Guiyang",
    "南宁": "Nanning", "海口": "Haikou", "拉萨": "Lhasa", "乌鲁木齐": "Urumqi",
    "香港": "Hong Kong", "台北": "Taipei", "澳门": "Macau",
}

WEATHER_KW = ["天气", "weather", "气温", "温度", "下雨", "刮风", "晴", "阴", "雨", "雪", "雾", "霾"]


def _route(query: str) -> str:
    """Provider router: weather -> wttr, everything else -> ddg."""
    if any(kw in query for kw in WEATHER_KW):
        return "weather"
    return "general"


async def run(**kwargs) -> dict:
    """Entry point — uses provider routing."""
    query = kwargs.get("query", "")
    if not query:
        return {"ok": False, "success": False, "output": "搜索失败", "error": "No query provided"}

    route_key = _route(query)
    if route_key == "weather":
        return await _search_wttr(query)
    else:
        return await _search_ddg(query)


async def _search_wttr(query: str) -> dict:
    """Weather via wttr.in."""
    city = "Shenzhen"
    for cn, en in CITY_MAP.items():
        if cn in query:
            city = en
            break

    try:
        url = "http://wttr.in/" + urllib.parse.quote(city) + "?format=j1"
        req = urllib.request.Request(url, headers={"User-Agent": "curl"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            import json
            data = json.loads(resp.read().decode())
            cur = data.get("current_condition", [{}])[0]
            weather = cur.get("weatherDesc", [{}])[0].get("value", "?")
            temp_c = cur.get("temp_C", "?")
            humidity = cur.get("humidity", "?")
            wind = cur.get("windspeedKmph", "?")
            feels = cur.get("FeelsLikeC", "?")

            forecast = data.get("weather", [])
            lines = []
            for day in forecast[:3]:
                date = day.get("date", "?")
                hi = day.get("maxtempC", "?")
                lo = day.get("mintempC", "?")
                desc = day.get("hourly", [{}])[4].get("weatherDesc", [{}])[0].get("value", "?")
                lines.append("  " + date + ": " + desc + " " + lo + "C~" + hi + "C")

            output = city + " current " + weather + " " + temp_c + "C (feels " + feels + "C)" + chr(10)
            output += "humidity " + humidity + "%  wind " + wind + "km/h"
            if lines:
                output += chr(10) + chr(10).join(lines)
            return {"ok": True, "success": True, "result": output}
    except Exception as e:
        return {"ok": False, "success": False, "output": "搜索失败", "error": "Weather failed: " + str(e)}


_PROXY = 'http://127.0.0.1:7890'


def _with_proxy_env(fn):
    """在**限定作用域**内临时设置代理环境变量, 用完无论成败都还原。

    ★ 2026-09-19 深测挖出的事故: 原实现在这里**永久**写 os.environ['HTTP_PROXY'] 且
      从不还原 → 同进程后续所有裸 urllib 请求(包括本工具的 Bing 兜底!)全都走代理 →
      代理返回的是无结果的精简页 → 触发 `_search_bing` 结果恒为空 →
      **web_search 对所有查询都返回 "(no results from Bing)" = 搜索能力整体失效**。
      (打桩实证: 经代理抓 cn.bing.com 只拿到 14.9KB 且不含 b_algo; 直连 97KB 含 10 条结果。)
      环境变量是**全局副作用**, 必须 try/finally 还原; 需要代理的只是 DDG 这一次调用。
    """
    import os as _os
    saved = {k: _os.environ.get(k) for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')}
    try:
        _os.environ['HTTP_PROXY'] = _os.environ['HTTPS_PROXY'] = _PROXY
        return fn()
    finally:
        for k, v in saved.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v


async def _search_ddg(query: str) -> dict:
    """DuckDuckGo search via ddgs library + mihomo proxy.

    ddgs 未安装或代理不可用 → 交给 Bing 兜底 (**必须**在代理 env 已还原的前提下)。
    """
    import logging as _log
    # Suppress ddgs/primp debug noise
    for _name in ('primp', 'httpx', 'ddgs', 'h2', 'httpcore'):
        _log.getLogger(_name).setLevel(_log.WARNING)
    try:
        from ddgs import DDGS
    except Exception:
        # ★ 没有 ddgs 就不要碰代理 env —— 直接交给 Bing (免得污染进程代理设置)
        return await _search_bing(query)
    try:
        def _query_ddg():
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=8, timelimit='m'))

        raw = _with_proxy_env(_query_ddg)
        # If no recent results, retry without time limit
        if not raw:
            def _query_ddg2():
                with DDGS() as ddgs:
                    return list(ddgs.text(query, max_results=8))
            raw = _with_proxy_env(_query_ddg2)
        results = []
        for r in raw:
            title = r.get('title', '').strip()
            href = r.get('href', '').strip()
            body = r.get('body', '').strip()
            if title and href.startswith('http'):
                results.append({"title": title, "url": href, "body": body})
        if not results:
            return await _search_bing(query)          # DDG 空 → Bing 兜底
        output = chr(10).join(
            str(i+1) + ". " + r["title"] + chr(10)
            + "   " + r["url"] + chr(10)
            + "   " + r.get("body", "")
            for i, r in enumerate(results)
        )
        return {"ok": True, "success": True, "result": output}
    except Exception:
        # DDG via proxy failed — fallback to Bing cn (代理 env 已还原)
        return await _search_bing(query)


async def _search_bing(query: str) -> dict:
    """Bing search — domestic, no proxy needed."""
    try:
        url = "https://cn.bing.com/search?q=" + urllib.parse.quote(query)
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
            results, seen = [], set()

            def _collect(pattern):
                for match in re.finditer(pattern, html, re.DOTALL):
                    url_found = match.group(1)
                    title = re.sub(r'<[^>]+>', '', match.group(2)).strip()
                    title = re.sub(r'\s+', ' ', title)
                    if title and url_found.startswith("http") and url_found not in seen:
                        seen.add(url_found)
                        results.append({"title": title, "url": url_found})
                        if len(results) >= 8:
                            return True
                return False

            # ① 结构化结果块 (老版 Bing) —— 匹配 <li class="b_algo"> … <h2><a href=…>标题</a>
            _collect(r'<li class="b_algo"[^>]*>.*?<h2[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>')
            # ② ★ 兜底: 直接抓正文区里的 h2>a 对 (Bing 改版/精简页时 ① 会失配,
            #    实测直连页两种都命中; 只靠 ① 会在页面结构变动时静默变"无结果")
            if len(results) < 3:
                _collect(r'<h2[^>]*>\s*<a[^>]*href="(http[^"]*)"[^>]*>(.*?)</a>')
            if not results:
                # 明确区分"没结果"与"被代理/反爬拦截后拿到空壳页"
                hint = "（页面异常: 拿到 " + str(len(html)) + " 字节且无结果块 —— 检查是否走了代理或触发了反爬）"
                return {"ok": True, "success": True, "result": "(no results from Bing) " + hint}
            output = chr(10).join(
                str(i+1) + ". " + r["title"] + chr(10) + "   " + r["url"]
                for i, r in enumerate(results)
            )
            return {"ok": True, "success": True, "result": output}
    except Exception as e:
        return {"ok": False, "success": False, "output": "搜索失败", "error": "Search failed: " + str(e)}
