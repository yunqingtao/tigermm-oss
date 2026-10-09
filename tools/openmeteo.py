"""
Open-Meteo Weather Plugin
Free, no API key, global coverage
Params: city (name), lat (float), lon (float), days (forecast days, default 1)
Uses Open-Meteo API v1: https://api.open-meteo.com/v1/forecast
"""

import urllib.request
import urllib.parse
import json
from typing import Optional, Dict, Any


# Built-in city coordinate cache — avoids Nominatim calls
CITY_COORDS = {
    "北京": (39.90, 116.40), "上海": (31.23, 121.47), "广州": (23.13, 113.26),
    "深圳": (22.54, 114.06), "杭州": (30.29, 120.15), "成都": (30.57, 104.07),
    "武汉": (30.58, 114.30), "南京": (32.06, 118.80), "西安": (34.26, 108.94),
    "重庆": (29.56, 106.55), "天津": (39.13, 117.20), "济南": (36.65, 117.10),
    "青岛": (36.07, 120.38), "大连": (38.91, 121.61), "苏州": (31.30, 120.62),
    "长沙": (28.23, 112.94), "郑州": (34.75, 113.63), "合肥": (31.82, 117.23),
    "福州": (26.07, 119.30), "厦门": (24.48, 118.09), "昆明": (25.04, 102.68),
    "哈尔滨": (45.80, 126.53), "沈阳": (41.80, 123.43), "长春": (43.90, 125.32),
    "石家庄": (38.04, 114.51), "太原": (37.87, 112.55), "兰州": (36.06, 103.83),
    "南宁": (22.82, 108.37), "贵阳": (26.65, 106.63), "海口": (20.02, 110.35),
    "南昌": (28.68, 115.86), "香港": (22.30, 114.17), "澳门": (22.20, 113.55),
    "台北": (25.05, 121.53), "东京": (35.68, 139.76), "首尔": (37.57, 126.98),
    "新加坡": (1.35, 103.82), "纽约": (40.71, -74.01), "伦敦": (51.51, -0.13),
    "Paris": (48.86, 2.35), "Berlin": (52.52, 13.40),
}


# WMO Weather codes → Chinese description
WEATHER_CN = {
    0: "晴朗", 1: "大部晴朗", 2: "多云", 3: "阴天",
    45: "有雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    71: "小雪", 73: "中雪", 75: "大雪",
    80: "阵雨", 81: "中等阵雨", 82: "大阵雨",
    85: "小阵雪", 86: "大阵雪",
    95: "雷暴", 96: "冰雹雷暴", 99: "强冰雹雷暴",
}

TOOL = {
    "name": "openmeteo",
    "keywords": ["天气", "气温", "温度", "下雨", "下雪", "几度", "weather", "气象"],
    "description": "获取天气——直接用city参数传城市名即可，如city=济南。不需要先调nominatim查坐标。免费无API Key",
    "params": [
        {"name": "city", "type": "string", "required": False, "description": "城市名称，如 Tokyo"},
        {"name": "lat", "type": "float", "required": False, "description": "纬度"},
        {"name": "lon", "type": "float", "required": False, "description": "经度"},
        {"name": "days", "type": "int", "required": False, "default": 1, "description": "预报天数（最多16天）"}
    ]
}

async def run(city: Optional[str] = None, lat: Optional[float] = None, lon: Optional[float] = None, days: int = 1, query: str = "", **kwargs) -> Dict[str, Any]:
    # /tool gateway passes query="city=北京" — parse if city is empty
    if query and not city:
        for part in query.replace("query=", "").split():
            if "=" in part:
                k, v = part.split("=", 1)
                v = v.strip("\"'")
                if k == "city":
                    city = v
                elif k == "lat":
                    lat = float(v)
                elif k == "lon":
                    lon = float(v)
                elif k == "days":
                    days = int(v)
    if city and (not lat or not lon):
        # Check built-in cache first
        if city in CITY_COORDS:
            lat, lon = CITY_COORDS[city]
        else:
            # Fall back to Nominatim geocoding
            try:
                geo_url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(city)}&format=json&limit=1"
                req = urllib.request.Request(geo_url, headers={"User-Agent": "MARY3-TigerMM/1.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    geo_data = json.loads(resp.read().decode())
                if geo_data:
                    lat = float(geo_data[0]["lat"])
                    lon = float(geo_data[0]["lon"])
                    CITY_COORDS[city] = (lat, lon)  # cache for future
                else:
                    return {"success": False, "output": "天气查询失败", "error": f"未找到城市: {city}"}
            except Exception as e:
                return {"success": False, "output": "天气查询失败", "error": f"地理编码失败: {str(e)}"}

    if lat is None or lon is None:
        return {"success": False, "output": "天气查询失败", "error": "请提供城市名或经纬度"}

    # Call Open-Meteo API
    forecast_days = min(max(1, days), 16)
    params = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "current_weather": "true",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode",
        "timezone": "auto",
        "forecast_days": forecast_days
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MARY3-TigerMM/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        
        # Extract current weather
        current = data.get("current_weather", {})
        daily = data.get("daily", {})
        
        result = {
            "success": True,
            "data": {
                "city": city or f"({lat:.2f}, {lon:.2f})",
                "lat": lat,
                "lon": lon,
                "current": {
                    "temperature": current.get("temperature"),
                    "windspeed": current.get("windspeed"),
                    "weathercode": current.get("weathercode"),
                    "time": current.get("time")
                },
                "daily": []
            }
        }
        
        # Build daily forecast first
        daily_data = []
        if daily:
            for i in range(len(daily.get("time", []))):
                day = {
                    "date": daily["time"][i],
                    "temp_max": daily["temperature_2m_max"][i],
                    "temp_min": daily["temperature_2m_min"][i],
                    "precipitation": daily["precipitation_sum"][i],
                    "weathercode": daily["weathercode"][i]
                }
                result["data"]["daily"].append(day)
                daily_data.append(day)
        
        # Build Chinese output
        cn_city = city or f"({lat:.1f}, {lon:.1f})"
        wcode = current.get("weathercode", 0)
        weather_cn = WEATHER_CN.get(wcode, f"天气码{wcode}")
        temp = current.get("temperature", "?")
        wind = current.get("windspeed", "?")
        out_lines = [
            f"{cn_city}天气",
            f"  {weather_cn} | {temp}°C | 风速 {wind}km/h",
        ]
        if daily_data:
            today = daily_data[0]
            out_lines.append(f"  今日: {today['temp_min']}°C ~ {today['temp_max']}°C")
            if today.get('precipitation', 0) > 0:
                out_lines.append(f"  降水: {today['precipitation']}mm")
            if len(daily_data) > 1:
                out_lines.append("")
                out_lines.append("  未来预报:")
                for d in daily_data[1:4]:
                    dw = WEATHER_CN.get(d['weathercode'], '?')
                    out_lines.append(f"    {d['date'][-5:]}: {dw} {d['temp_min']}~{d['temp_max']}°C")
        result["output"] = "\n".join(out_lines)
        
        return result
    
    except urllib.error.HTTPError as e:
        return {"success": False, "output": "天气查询失败", "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"success": False, "output": "天气查询失败", "error": str(e)}