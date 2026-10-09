"""
Hermes Kernel v1.0 — Web Reverse-Engineering & Anti-Crawl Engine
Full capability transfer to Tiger.M.M
Contains: crawl planning, anti-detection, JS parsing, headless control,
API reverse-engineering, form automation, captcha handling, traffic伪装
"""

import re, json, time, random, hashlib, urllib.parse, urllib.request
import ssl, http.cookiejar, gzip, io, threading, queue
from collections import deque
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field

# === SECTION 1: STEALTH HTTP CLIENT ===

@dataclass
class StealthConfig:
    """Anti-detection configuration"""
    rotate_ua: bool = True
    random_delay: Tuple[float, float] = (0.5, 3.0)
    max_retries: int = 3
    retry_backoff: float = 2.0
    max_concurrent: int = 5
    proxy_pool: List[str] = field(default_factory=list)
    cookie_jar: bool = True
    follow_redirects: bool = True
    impersonate_browser: bool = True

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
]

BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

class StealthSession:
    """Anti-detection HTTP session with browser impersonation"""
    
    def __init__(self, config: StealthConfig = None):
        self.config = config or StealthConfig()
        self.cj = http.cookiejar.CookieJar()
        self.ctx = ssl.create_default_context()
        self.last_request_time = 0
        self.consecutive_errors = 0
        self.proxy_index = 0
        self.session_fingerprint = self._generate_fingerprint()
        
    def _generate_fingerprint(self) -> str:
        """Generate unique browser fingerprint per session"""
        return hashlib.sha256(
            f"{random.random()}-{time.time()}-{random.randint(0,999999)}".encode()
        ).hexdigest()[:32]
    
    def _get_headers(self, url: str, referer: str = None) -> Dict[str, str]:
        """Build stealth request headers"""
        headers = dict(BROWSER_HEADERS)
        if self.config.impersonate_browser:
            headers["User-Agent"] = random.choice(USER_AGENTS)
        if referer:
            headers["Referer"] = referer
        headers["X-Forwarded-For"] = f"{random.randint(1,255)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"
        return headers
    
    def _enforce_delay(self):
        """Rate limiting with randomized delay"""
        elapsed = time.time() - self.last_request_time
        min_d, max_d = self.config.random_delay
        needed = random.uniform(min_d, max_d) - elapsed
        if needed > 0:
            time.sleep(needed)
    
    def _rotate_proxy(self) -> Optional[str]:
        """Rotate through proxy pool"""
        if not self.config.proxy_pool:
            return None
        proxy = self.config.proxy_pool[self.proxy_index % len(self.config.proxy_pool)]
        self.proxy_index += 1
        return proxy
    
    def request(self, url: str, method: str = "GET", data: bytes = None,
                headers: Dict = None, referer: str = None,
                timeout: int = 30) -> Tuple[int, bytes, Dict]:
        """Make a stealth HTTP request with full anti-detection"""
        self._enforce_delay()
        
        all_headers = self._get_headers(url, referer)
        if headers:
            all_headers.update(headers)
        
        for attempt in range(self.config.max_retries):
            try:
                proxy = self._rotate_proxy()
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy else urllib.request.ProxyHandler({}),
                    urllib.request.HTTPCookieProcessor(self.cj) if self.config.cookie_jar else urllib.request.HTTPCookieProcessor(),
                    urllib.request.HTTPSHandler(context=self.ctx),
                )
                
                req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
                resp = opener.open(req, timeout=timeout)
                
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                
                self.last_request_time = time.time()
                self.consecutive_errors = 0
                
                return resp.status, body, dict(resp.headers)
                
            except urllib.error.HTTPError as e:
                status = e.code
                if status == 429:
                    wait = int(e.headers.get("Retry-After", self.config.retry_backoff * (2 ** attempt)))
                    time.sleep(wait)
                elif status in (403, 503):
                    time.sleep(self.config.retry_backoff * (attempt + 1))
                else:
                    break
            except Exception as e:
                self.consecutive_errors += 1
                time.sleep(self.config.retry_backoff * (attempt + 1))
        
        return 0, b"", {}
    
    def get(self, url: str, **kwargs) -> Tuple[int, str, Dict]:
        status, body, headers = self.request(url, "GET", **kwargs)
        encoding = self._detect_encoding(body, headers)
        return status, body.decode(encoding, errors="ignore"), headers
    
    def post(self, url: str, data: dict = None, json_data: dict = None, **kwargs):
        if json_data:
            body = urllib.parse.urlencode(json_data).encode()
            kwargs.setdefault("headers", {})["Content-Type"] = "application/json"
        elif data:
            body = urllib.parse.urlencode(data).encode()
        else:
            body = b""
        return self.request(url, "POST", body, **kwargs)
    
    def _detect_encoding(self, body: bytes, headers: Dict) -> str:
        """Auto-detect page encoding"""
        content_type = headers.get("Content-Type", "")
        for part in content_type.split(";"):
            if "charset" in part.lower():
                return part.split("=")[-1].strip().lower()
        sample = body[:2000].decode("utf-8", errors="ignore")
        m = re.search(r'charset=["\']?([a-zA-Z0-9\-_]+)', sample)
        if m:
            return m.group(1).lower()
        return "utf-8"


# === SECTION 2: JS PARSING & API REVERSE-ENGINEERING ===

@dataclass
class APIEndpoint:
    """Discovered API endpoint"""
    url: str
    method: str = "GET"
    params: Dict = field(default_factory=dict)
    headers: Dict = field(default_factory=dict)
    body_template: Dict = field(default_factory=dict)
    response_sample: str = ""
    auth_type: str = ""

class JSReverseEngineer:
    """Extract API endpoints, tokens, and logic from JavaScript"""
    
    API_PATTERNS = [
        r'(?:fetch|axios|ajax|XMLHttpRequest)\s*\(\s*["\']([^"\']+)["\']',
        r'(?:url|api|endpoint|baseURL|base_url|BASE_URL)\s*[:=]\s*["\']([^"\']+)["\']',
        r'(?:http|https)://[^\s"\'<>]+?(?:/api/|/v\d/|/graphql)[^\s"\'<>]*',
        r'["\']((?:/api/|/v\d/|/graphql|/ajax/)[^"\'\s]+)["\']',
        r'(?:post|get|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']',
        r'(?:\.post\(|\.get\(|\.put\(|\.delete\()\s*["\']([^"\']+)["\']',
    ]
    
    TOKEN_PATTERNS = [
        r'(?:token|auth|bearer|apiKey|api_key|apikey|secret|sign)\s*[:=]\s*["\']([^"\']+)["\']',
        r'(?:authorization|Authorization)\s*[:=]\s*["\'](Bearer\s+)?([^"\']+)["\']',
        r'["\']([\w\-_]{20,})["\']\s*[:=]\s*["\']([\w\-_]{20,})["\']',
        r'localStorage\.(?:get|set)Item\s*\(\s*["\']([^"\']+)["\']',
    ]
    
    @classmethod
    def extract_endpoints(cls, js_code: str) -> List[APIEndpoint]:
        """Extract all API endpoints from JavaScript"""
        endpoints = []
        seen = set()
        for pattern in cls.API_PATTERNS:
            for m in re.finditer(pattern, js_code, re.IGNORECASE):
                url = m.group(1)
                if url not in seen and len(url) > 2:
                    seen.add(url)
                    endpoints.append(APIEndpoint(url=url))
        return endpoints
    
    @classmethod
    def extract_tokens(cls, js_code: str) -> Dict[str, str]:
        """Extract auth tokens and secrets from JS"""
        tokens = {}
        for pattern in cls.TOKEN_PATTERNS:
            for m in re.finditer(pattern, js_code, re.IGNORECASE):
                key = m.group(1) if m.lastindex >= 1 else m.group(0)[:30]
                val = m.group(m.lastindex) if m.lastindex > 1 else m.group(0)
                if len(val) > 8:
                    tokens[key] = val
        return tokens
    
    @classmethod
    def extract_json_data(cls, html: str) -> List[dict]:
        """Extract embedded JSON data from HTML (__NEXT_DATA__, __NUXT__, etc.)"""
        data = []
        json_patterns = [
            r'__NEXT_DATA__\s*=\s*({.+?});',
            r'__NUXT__\s*=\s*({.+?});',
            r'window\.__INITIAL_STATE__\s*=\s*({.+?});',
            r'<script[^>]*type="application/json"[^>]*>(.+?)</script>',
            r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.+?)</script>',
        ]
        for pattern in json_patterns:
            for m in re.finditer(pattern, html, re.DOTALL):
                try:
                    data.append(json.loads(m.group(1)))
                except Exception:
                    pass
        return data


# === SECTION 3: PAGE STRUCTURE MAPPING ===

@dataclass
class PageElement:
    tag: str
    attrs: Dict[str, str]
    text: str = ""
    xpath: str = ""
    children: List["PageElement"] = field(default_factory=list)

class PageMapper:
    """Autonomous site structure mapping and element traversal"""
    
    @classmethod
    def extract_links(cls, html: str, base_url: str = "") -> List[str]:
        """Extract all meaningful links from page"""
        links = set()
        for m in re.finditer(r'href=["\']([^"\']+)["\']', html):
            href = m.group(1)
            if href.startswith("#") or href.startswith("javascript:"):
                continue
            if not href.startswith("http"):
                href = urllib.parse.urljoin(base_url, href)
            links.add(href)
        return list(links)
    
    @classmethod
    def extract_forms(cls, html: str) -> List[Dict]:
        """Extract all forms with their fields"""
        forms = []
        for m in re.finditer(r'<form[^>]*?(?:action=["\']([^"\']*)["\'])?[^>]*>(.+?)</form>', html, re.DOTALL | re.IGNORECASE):
            action = m.group(1) or ""
            form_body = m.group(2)
            inputs = []
            for im in re.finditer(r'<(?:input|select|textarea)[^>]*?>', form_body, re.IGNORECASE):
                tag = im.group(0)
                name = re.search(r'name=["\']([^"\']+)["\']', tag)
                type_ = re.search(r'type=["\']([^"\']+)["\']', tag)
                value = re.search(r'value=["\']([^"\']*)["\']', tag)
                inputs.append({
                    "name": name.group(1) if name else "",
                    "type": type_.group(1) if type_ else "text",
                    "value": value.group(1) if value else "",
                })
            forms.append({"action": action, "fields": inputs})
        return forms
    
    @classmethod
    def map_site_structure(cls, html: str) -> Dict[str, Any]:
        """Autonomous site mapping: navigation, sections, data zones"""
        structure = {
            "title": "",
            "meta": {},
            "nav_links": [],
            "sections": [],
            "data_tables": [],
            "scripts": [],
            "images": [],
        }
        
        title_m = re.search(r'<title[^>]*>(.+?)</title>', html, re.IGNORECASE)
        if title_m:
            structure["title"] = title_m.group(1)
        
        for m in re.finditer(r'<meta[^>]*name=["\']([^"\']+)["\'][^>]*content=["\']([^"\']+)["\']', html, re.IGNORECASE):
            structure["meta"][m.group(1)] = m.group(2)
        
        for m in re.finditer(r'<(?:nav|header)[^>]*>(.+?)</(?:nav|header)>', html, re.DOTALL | re.IGNORECASE):
            structure["nav_links"].extend(re.findall(r'href=["\']([^"\']+)["\']', m.group(1)))
        
        for m in re.finditer(r'<table[^>]*>(.+?)</table>', html, re.DOTALL | re.IGNORECASE):
            structure["data_tables"].append(m.group(1)[:1000])
        
        for m in re.finditer(r'<script[^>]*src=["\']([^"\']+)["\']', html, re.IGNORECASE):
            structure["scripts"].append(m.group(1))
        
        for m in re.finditer(r'<img[^>]*src=["\']([^"\']+)["\']', html, re.IGNORECASE):
            structure["images"].append(m.group(1))
        
        return structure


# === SECTION 4: CRAWL PLANNING ENGINE ===

@dataclass
class CrawlTask:
    url: str
    priority: int = 5
    depth: int = 0
    parent_url: str = ""
    extract_rules: Dict = field(default_factory=dict)
    retry_count: int = 0
    status: str = "pending"

class CrawlPlanner:
    """Autonomous crawl planning with priority queue and adaptive strategy"""
    
    def __init__(self, session: StealthSession = None):
        self.session = session or StealthSession()
        self.queue = deque()
        self.visited = set()
        self.results = []
        self.rate_limiter = {}
        self.domain_delays = {}
        self.failed_urls = {}
        
    def plan_crawl(self, seed_urls: List[str], max_depth: int = 3,
                   max_pages: int = 100, domain: str = None) -> List[CrawlTask]:
        """Generate autonomous crawl plan"""
        tasks = []
        for url in seed_urls:
            domain = domain or urllib.parse.urlparse(url).netloc
            for depth in range(max_depth):
                tasks.append(CrawlTask(
                    url=url if depth == 0 else "",
                    priority=max(1, 10 - depth),
                    depth=depth,
                ))
        return sorted(tasks, key=lambda t: (-t.priority, t.depth))
    
    def add_to_queue(self, url: str, depth: int = 0, parent: str = ""):
        """Add URL to crawl queue with dedup"""
        normalized = self._normalize_url(url)
        if normalized not in self.visited:
            self.queue.append(CrawlTask(url=url, depth=depth, parent_url=parent))
    
    def _normalize_url(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    
    def adaptive_delay(self, domain: str, response_time: float):
        """Adaptive rate limiting based on server response time"""
        if domain not in self.domain_delays:
            self.domain_delays[domain] = 1.0
        if response_time > 3.0:
            self.domain_delays[domain] = min(10.0, self.domain_delays[domain] * 1.5)
        elif response_time < 0.5 and self.domain_delays[domain] > 0.5:
            self.domain_delays[domain] = max(0.3, self.domain_delays[domain] * 0.8)
        time.sleep(self.domain_delays[domain])
    
    def should_retry(self, url: str, status: int) -> bool:
        """Decide whether to retry based on error type"""
        if status in (429, 503):
            return True
        if status == 403:
            key = urllib.parse.urlparse(url).netloc
            self.failed_urls[key] = self.failed_urls.get(key, 0) + 1
            return self.failed_urls[key] < 3
        return False
    
    def execute_plan(self, tasks: List[CrawlTask]) -> List[Dict]:
        """Execute crawl plan autonomously"""
        for task in tasks:
            if task.url in self.visited:
                continue
            
            domain = urllib.parse.urlparse(task.url).netloc
            t0 = time.time()
            
            status, html, headers = self.session.get(task.url)
            
            self.adaptive_delay(domain, time.time() - t0)
            
            if status == 200:
                self.visited.add(task.url)
                self.results.append({
                    "url": task.url, "depth": task.depth,
                    "status": status, "size": len(html),
                    "links": PageMapper.extract_links(html, task.url),
                    "forms": PageMapper.extract_forms(html),
                    "title": re.search(r'<title[^>]*>(.+?)</title>', html, re.IGNORECASE),
                })
            elif self.should_retry(task.url, status):
                task.retry_count += 1
                if task.retry_count < 3:
                    self.queue.append(task)
        
        return self.results


# === SECTION 5: ANTI-BLOCK ADAPTIVE STRATEGIES ===

class AntiBlockEngine:
    """Adaptive anti-blocking: detect blocks, rotate identity, evade detection"""
    
    BLOCK_SIGNALS = [
        (r"captcha|verify|robot|human|block|forbidden|denied|challenge", "captcha"),
        (r"rate.?limit|too.?many|slow.?down|throttle", "rate_limit"),
        (r"cloudflare|cf-|__cf|challenges", "cloudflare"),
        (r"access.?denied|unauthorized|not.?allowed", "access_denied"),
        (r"maintenance|temporarily|try.?again|later", "temp_block"),
    ]
    
    def __init__(self):
        self.block_count = {}
        self.evasion_level = 0
        self.current_strategy = "normal"
    
    def detect_block(self, html: str, status: int) -> Optional[str]:
        """Detect if page is blocking us"""
        html_lower = html.lower()[:5000]
        
        if status == 403 and len(html) < 500:
            return "access_denied"
        if status == 429:
            return "rate_limit"
        if status == 503:
            return "temp_block"
        
        for pattern, block_type in self.BLOCK_SIGNALS:
            if re.search(pattern, html_lower):
                return block_type
        
        return None
    
    def adapt_strategy(self, block_type: str, session: StealthSession):
        """Adapt evasion strategy based on block type"""
        self.evasion_level = min(5, self.evasion_level + 1)
        
        strategies = {
            "captcha": self._evade_captcha,
            "rate_limit": self._evade_rate_limit,
            "cloudflare": self._evade_cloudflare,
            "access_denied": self._evade_access_denied,
            "temp_block": self._evade_temp_block,
        }
        
        handler = strategies.get(block_type, self._evade_generic)
        return handler(session)
    
    def _evade_captcha(self, session: StealthSession):
        """CAPTCHA evasion: rotate fingerprint, clear cookies, add delay"""
        session.cj.clear()
        session.session_fingerprint = session._generate_fingerprint()
        session.config.random_delay = (3.0, 8.0)
        self.current_strategy = "captcha_evasion"
    
    def _evade_rate_limit(self, session: StealthSession):
        """Rate limit evasion: exponential backoff"""
        session.config.random_delay = (
            session.config.random_delay[0] * 2,
            session.config.random_delay[1] * 2,
        )
        self.current_strategy = "rate_limit_backoff"
    
    def _evade_cloudflare(self, session: StealthSession):
        """Cloudflare evasion: impersonate standard browser perfectly"""
        session.config.impersonate_browser = True
        session.config.cookie_jar = True
        session.config.random_delay = (2.0, 5.0)
        self.current_strategy = "cloudflare_evasion"
    
    def _evade_access_denied(self, session: StealthSession):
        """Access denied: rotate identity completely"""
        session.cj.clear()
        session.session_fingerprint = session._generate_fingerprint()
        session.config.rotate_ua = True
        self.current_strategy = "identity_rotation"
    
    def _evade_temp_block(self, session: StealthSession):
        """Temporary block: long wait then retry"""
        wait = 30 * (2 ** min(self.evasion_level, 3))
        time.sleep(wait)
        self.current_strategy = "temp_block_wait"
    
    def _evade_generic(self, session: StealthSession):
        """Generic evasion: rotate everything"""
        session.cj.clear()
        session.session_fingerprint = session._generate_fingerprint()
        session.config.random_delay = (2.0, 6.0)


# === SECTION 6: HEADLESS BROWSER CONTROL PATTERNS ===

class HeadlessController:
    """Headless browser patterns (usable with Selenium/Playwright/Puppeteer)"""
    
    STEALTH_JS = """
    // Override navigator properties
    Object.defineProperty(navigator, 'webdriver', { get: () => false });
    Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh','en'] });
    
    // Override permissions
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ?
        Promise.resolve({state: Notification.permission}) :
        originalQuery(parameters)
    );
    
    // Hide headless indicators
    delete window.__webdriver_elements;
    delete window.__selenium_evaluator;
    delete window.__selenium_unwrapped;
    """
    
    @staticmethod
    def selenium_stealth_config() -> Dict:
        """Return Selenium stealth configuration"""
        return {
            "excludeSwitches": ["enable-automation"],
            "useAutomationExtension": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                f"--user-agent={random.choice(USER_AGENTS)}",
            ],
        }
    
    @staticmethod
    def wait_for_element_patterns() -> List[str]:
        """CSS selectors for detecting page load completion"""
        return [
            "body", "main", "#app", "#root", "[data-reactroot]",
            "table", "ul", ".content", ".main-content",
            "[data-loaded]", ".loaded", ".ready",
        ]
    
    @staticmethod
    def extract_dynamic_content_js() -> str:
        """JavaScript to extract all visible text and data from page"""
        return """
        const data = {
            title: document.title,
            url: window.location.href,
            text: document.body.innerText.substring(0, 50000),
            links: Array.from(document.querySelectorAll('a[href]')).map(a => ({
                text: a.innerText.trim().substring(0, 100),
                href: a.href
            })),
            tables: Array.from(document.querySelectorAll('table')).map(t => ({
                rows: t.rows.length,
                headers: Array.from(t.rows[0]?.cells || []).map(c => c.innerText)
            })),
            images: Array.from(document.querySelectorAll('img[src]')).map(i => ({
                src: i.src,
                alt: i.alt
            })).slice(0, 50),
            forms: Array.from(document.querySelectorAll('form')).map(f => ({
                action: f.action,
                method: f.method,
                inputs: Array.from(f.querySelectorAll('input,select,textarea')).map(i => ({
                    name: i.name, type: i.type, value: i.value
                }))
            }))
        };
        return JSON.stringify(data);
        """


# === SECTION 7: TRAFFIC SNIFFING & API CAPTURE PATTERNS ===

class TrafficSniffer:
    """Network traffic analysis patterns for API discovery"""
    
    @staticmethod
    def analyze_xhr_patterns(js_code: str) -> Dict[str, Any]:
        """Analyze XHR/AJAX call patterns in JS"""
        patterns = {
            "fetch_calls": [],
            "axios_calls": [],
            "xhr_calls": [],
            "websocket_calls": [],
            "graphql_queries": [],
        }
        
        for m in re.finditer(r'fetch\s*\(\s*["\']([^"\']+)["\']', js_code):
            patterns["fetch_calls"].append(m.group(1))
        for m in re.finditer(r'axios\.(?:get|post|put|delete)\s*\(\s*["\']([^"\']+)["\']', js_code):
            patterns["axios_calls"].append(f"{m.group(0)}")
        for m in re.finditer(r'new WebSocket\s*\(\s*["\']([^"\']+)["\']', js_code):
            patterns["websocket_calls"].append(m.group(1))
        for m in re.finditer(r'(?:query|mutation)\s*\{[^}]*\}', js_code):
            patterns["graphql_queries"].append(m.group(0)[:200])
        
        return patterns
    
    @staticmethod
    def build_request_from_har(har_entry: Dict) -> Dict:
        """Convert HAR entry to actionable request"""
        request = har_entry.get("request", {})
        return {
            "url": request.get("url", ""),
            "method": request.get("method", "GET"),
            "headers": {h["name"]: h["value"] for h in request.get("headers", [])},
            "query_string": {q["name"]: q["value"] for q in request.get("queryString", [])},
            "post_data": request.get("postData", {}).get("text", ""),
        }
    
    @staticmethod
    def detect_api_pattern(urls: List[str]) -> Dict[str, List[str]]:
        """Categorize discovered API endpoints"""
        categories = {
            "rest": [], "graphql": [], "auth": [], "data": [],
            "upload": [], "search": [], "admin": [], "config": [],
        }
        for url in urls:
            u = url.lower()
            if "graphql" in u: categories["graphql"].append(url)
            elif any(k in u for k in ["login", "auth", "token", "oauth", "signin"]): categories["auth"].append(url)
            elif any(k in u for k in ["upload", "file"]): categories["upload"].append(url)
            elif any(k in u for k in ["search", "query", "find"]): categories["search"].append(url)
            elif any(k in u for k in ["admin", "manage", "dashboard"]): categories["admin"].append(url)
            elif any(k in u for k in ["config", "setting", "option"]): categories["config"].append(url)
            elif any(k in u for k in ["/api/", "/v1/", "/v2/"]): categories["rest"].append(url)
            else: categories["data"].append(url)
        return {k: v for k, v in categories.items() if v}


# === SECTION 8: AUTONOMOUS THINKING PARADIGM ===

HERMES_THINKING_PARADIGM = {
    "decision_architecture": {
        "phase1_assess": "Evaluate target: static/dynamic, auth required, JS-dependent, rate-limited",
        "phase2_strategize": "Choose approach: direct HTTP, headless browser, API reverse, hybrid",
        "phase3_probe": "Lightweight probe: HEAD request, robots.txt, sitemap.xml, JS bundle analysis",
        "phase4_extract": "Execute extraction: crawl, parse, transform, validate",
        "phase5_adapt": "Monitor block signals, rotate identity, adjust strategy in real-time",
        "phase6_verify": "Validate data completeness, retry failed items, cross-reference sources",
    },
    "safety_thresholds": {
        "max_requests_per_domain_per_minute": 30,
        "max_concurrent_connections": 8,
        "max_page_depth": 5,
        "max_retry_per_url": 3,
        "politeness_delay_min": 0.5,
        "respect_robots_txt": True,
        "max_data_extract_bytes": 50_000_000,
        "block_detection_sensitivity": 0.7,
    },
    "autonomous_strategies": {
        "login_wall": ["credential rotation", "session reuse", "OAuth token refresh"],
        "js_rendered_content": ["API call reverse-engineering", "SSR data extraction", "WebSocket sniffing"],
        "infinite_scroll": ["API pagination detection", "cursor-based pagination", "offset calculation"],
        "ip_blocking": ["proxy rotation", "delay randomization", "request pattern randomization"],
        "captcha": ["audio fallback", "third-party solving service", "session hijacking"],
    },
    "traffic_masquerade": {
        "fingerprint_randomization": ["user-agent", "screen resolution", "timezone", "language", "fonts"],
        "behavioral_mimicry": ["mouse movements", "scroll patterns", "typing delays", "tab switching"],
        "network_patterns": ["request ordering", "subresource loading", "connection reuse", "TLS fingerprint"],
    },
}

# Export manifest
HERMES_KERNEL_MANIFEST = {
    "name": "Hermes Kernel",
    "version": "1.0",
    "modules": [
        "StealthSession - Anti-detection HTTP client",
        "JSReverseEngineer - JS parsing and API reverse-engineering",
        "PageMapper - Site structure mapping and element traversal",
        "CrawlPlanner - Autonomous crawl planning and execution",
        "AntiBlockEngine - Adaptive anti-blocking strategies",
        "HeadlessController - Headless browser control patterns",
        "TrafficSniffer - Network traffic analysis and API capture",
    ],
    "thinking_paradigm": HERMES_THINKING_PARADIGM,
    "built_in_commands": {
        "stealth_get": "Make anti-detection HTTP request",
        "extract_endpoints": "Parse JS for API endpoints",
        "map_site": "Autonomous site structure mapping",
        "plan_crawl": "Generate crawl plan from seed URLs",
        "detect_block": "Detect if page is blocking",
        "extract_forms": "Extract all forms from page",
        "sniff_apis": "Analyze traffic patterns for APIs",
    },
}