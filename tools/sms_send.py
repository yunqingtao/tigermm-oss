"""
SMS Plugin - Tencent Cloud SMS
===============================
Requires: tencentcloud-sdk-python (already installed)

Setup:
  1. Register at https://console.cloud.tencent.com/smsv2
  2. Get SecretId + SecretKey from https://console.cloud.tencent.com/cam/capi
  3. Create signature + template, get IDs
  4. Fill in below config
"""
import json as _json

PLUGIN = {
    "name": "sms_send",
    "description": "Send SMS via Tencent Cloud",
    "version": "1.0",
    "requires": ["tencentcloud-sdk-python"],
    "trigger": ["sms", "text message", "send sms"],
    "permission": ["system"],
    "category": "messaging",
}

# --- Configuration ---
# ★ 2026-09-20 分发整改: 凭据**移出源码**。原 secret_id/secret_key 是明文写死在这里的,
#   而本文件被 git 跟踪 → 一旦发布即泄露 (与 tools/send_email.py 同类问题)。
#   现在读取顺序: 环境变量 → keys.json["sms"]; 拿不到就**诚实报错**。
#   ⚠ 历史里那两个值应视为已暴露 → 建议去腾讯云控制台**轮换**。
def _sms_cfg() -> dict:
    import json as _json_sms
    import os as _os_sms
    from pathlib import Path as _Path_sms
    cfg = {"secret_id": _os_sms.environ.get("TMM_SMS_SECRET_ID", ""),
           "secret_key": _os_sms.environ.get("TMM_SMS_SECRET_KEY", ""),
           "app_id": _os_sms.environ.get("TMM_SMS_APP_ID", ""),
           "sign_name": "", "template_id": ""}
    if not (cfg["secret_id"] and cfg["secret_key"]):
        try:
            with open(_Path_sms(__file__).resolve().parent.parent / "keys.json", encoding="utf-8") as f:
                d = _json_sms.load(f).get("sms") or {}
            for k in ("secret_id", "secret_key", "app_id", "sign_name", "template_id"):
                cfg[k] = cfg[k] or str(d.get(k, "") or "")
        except Exception:
            pass
    return cfg


def _sms_ready() -> tuple[bool, str]:
    c = _sms_cfg()
    if not (c["secret_id"] and c["secret_key"]):
        return False, ("短信没配置。请在 keys.json 加一段:\n"
                       '  "sms": {"secret_id": "…", "secret_key": "…", "app_id": "…",\n'
                       '          "sign_name": "…", "template_id": "…"}\n'
                       "或设环境变量 TMM_SMS_SECRET_ID / TMM_SMS_SECRET_KEY。")
    return True, ""


# Check SDK
try:
    from tencentcloud.common import credential
    from tencentcloud.sms.v20210111 import sms_client, models
    _SMS_READY = True
except ImportError:
    _SMS_READY = False


async def run(**kwargs) -> dict:
    action = kwargs.get("action", "send")

    if action == "send":
        return _send_sms(kwargs)
    elif action == "status":
        return {
            "success": True,
            "output": {
                "sdk_ready": _SMS_READY,
                "configured": bool(_sms_cfg()["secret_id"] and _sms_cfg()["app_id"]),
            }
        }
    return {"success": False, "error": "Unknown action: " + action}


def _send_sms(kwargs) -> dict:
    if not _SMS_READY:
        return {"success": False, "error": "tencentcloud-sdk-python not installed"}
    if not _sms_cfg()["secret_id"]:
        return {"success": False, "error": "SMS not configured - set secret_id/secret_key/app_id"}

    phones = kwargs.get("phones", kwargs.get("to", ""))
    if isinstance(phones, str):
        phones = [p.strip() for p in phones.split(",") if p.strip()]
    if not phones:
        return {"success": False, "error": "Phone number(s) required"}

    # Normalize: add +86 prefix if missing
    normalized = []
    for p in phones:
        p = p.strip()
        if not p.startswith("+"):
            p = "+86" + p
        normalized.append(p)

    template_params = kwargs.get("params", kwargs.get("template_params", []))
    if isinstance(template_params, str):
        template_params = template_params.split(",")

    try:
        cred = credential.Credential(_sms_cfg()["secret_id"], _sms_cfg()["secret_key"])
        client = sms_client.SmsClient(cred, "ap-guangzhou")

        req = models.SendSmsRequest()
        req.SmsSdkAppId = _sms_cfg()["app_id"]
        req.SignName = _sms_cfg()["sign_name"]
        req.TemplateId = _sms_cfg()["template_id"]
        req.TemplateParamSet = template_params
        req.PhoneNumberSet = normalized

        resp = client.SendSms(req)
        result = _json.loads(resp.to_json_string())

        sent = []
        failed = []
        for status in result.get("SendStatusSet", []):
            phone = status.get("PhoneNumber", "?")
            if status.get("Code") == "Ok":
                sent.append(phone)
            else:
                failed.append(phone + ": " + status.get("Message", "?"))

        return {
            "success": True,
            "output": "SMS sent: " + str(len(sent)) + " OK" +
                      (", " + str(len(failed)) + " failed" if failed else ""),
            "sent": sent,
            "failed": failed,
            "request_id": result.get("RequestId", ""),
        }
    except Exception as e:
        return {"success": False, "error": "SMS send failed: " + str(e)}
