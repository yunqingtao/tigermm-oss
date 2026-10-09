"""
Nominatim Geocoding Plugin (OpenStreetMap)
Free, no API key required. Forward: city->coords. Reverse: coords->address.
"""
import urllib.request, urllib.parse, json, time, os

TOOL = {
    "name": "nominatim",
    "description": "Geocode city name to coordinates (lat/lon) or reverse geocode",
    "keywords": ["坐标", "经纬度", "经度", "纬度", "geocode", "地理位置"],
    "params": [
        {"name": "query", "type": "string", "description": "City name or address"},
        {"name": "city", "type": "string", "description": "City name (alias for query)"},
        {"name": "lat", "type": "float", "description": "Latitude for reverse geocode"},
        {"name": "lon", "type": "float", "description": "Longitude for reverse geocode"},
    ],
}

_last_time = 0
_USER_AGENT = "TMM_Link/1.0"

def _get_opener():
    """Build opener — try proxy first, fall back to direct."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
    return urllib.request.build_opener()

async def run(**kwargs) -> dict:
    global _last_time
    query = kwargs.get("query") or kwargs.get("city") or kwargs.get("q", "")
    lat = kwargs.get("lat")
    lon = kwargs.get("lon")

    # Rate limit
    now = time.time()
    if now - _last_time < 1.0:
        time.sleep(1.0 - (now - _last_time))
    _last_time = time.time()

    if lat is not None and lon is not None:
        url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json"
    elif query:
        url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(query)}&format=json&limit=1"
    else:
        return {"success": False, "output": "地理编码失败", "error": "Need query/city or lat+lon"}

    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with _get_opener().open(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list):
                if not data:
                    return {"success": False, "output": "地理编码失败", "error": f"'{query}' not found"}
                r = data[0]
                return {"success": True, "data": {"lat": float(r["lat"]), "lon": float(r["lon"]), "name": r.get("display_name", query)}}
            return {"success": True, "data": {"lat": float(data.get("lat", 0)), "lon": float(data.get("lon", 0)), "name": data.get("display_name", "")}}
    except Exception as e:
        return {"success": False, "output": "地理编码失败", "error": str(e)}
