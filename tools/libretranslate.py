"""
Translation plugin — Google Translate (free unofficial API).
"""
import urllib.request, urllib.parse, json

TOOL = {
    "name": "libretranslate",
    "description": "Translate text using Google Translate free API. Use for 翻译, translate, 中英互译.",
    "keywords": ["翻译", "translate", "译", "中英", "英文", "中文"],
    "params": [
        {"name": "text", "type": "str", "description": "Text to translate"},
        {"name": "source", "type": "str", "default": "auto", "description": "Source language code or 'auto'"},
        {"name": "target", "type": "str", "default": "en", "description": "Target language code"},
    ]
}

BASE_URL = "https://translate.googleapis.com/translate_a/single"

async def run(text: str, source: str = "auto", target: str = "en", **kwargs) -> dict:
    try:
        params = {
            "client": "gtx",
            "sl": source,
            "tl": target,
            "dt": "t",
            "q": text,
        }
        url = BASE_URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        
        # Google Translate returns: [[["translated","original",...]],...]
        translated = "".join(part[0] for part in data[0] if part[0])
        
        return {
            "success": True,
            "data": {
                "translatedText": translated,
                "detectedLanguage": {"language": data[2] if len(data) > 2 else source},
            }
        }
    except Exception as e:
        return {"success": False, "output": "翻译失败", "error": str(e)}
