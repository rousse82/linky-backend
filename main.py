"""
Linky Dashboard — Backend
Interroge l'API Tuya toutes les 10 secondes
et insère les données dans Supabase.
"""

import os
import time
import hmac
import hashlib
import requests
from datetime import datetime, timezone
from supabase import create_client

# ── Config (variables d'environnement Railway) ──────────────────────────────
TUYA_CLIENT_ID     = os.environ["TUYA_CLIENT_ID"]
TUYA_CLIENT_SECRET = os.environ["TUYA_CLIENT_SECRET"]
TUYA_DEVICE_ID     = os.environ["TUYA_DEVICE_ID"]
TUYA_BASE_URL      = os.environ.get("TUYA_BASE_URL", "https://openapi.tuyaeu.com")

SUPABASE_URL       = os.environ["SUPABASE_URL"]
SUPABASE_KEY       = os.environ["SUPABASE_KEY"]   # clé service_role

POLL_INTERVAL      = int(os.environ.get("POLL_INTERVAL", "10"))  # secondes

# ── Supabase ─────────────────────────────────────────────────────────────────
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Tuya Auth ─────────────────────────────────────────────────────────────────
_token_cache = {"token": None, "expires_at": 0}

def _sign(method, path, body="", token=""):
    ts = str(int(time.time() * 1000))
    content_hash = hashlib.sha256(body.encode()).hexdigest()
    str_to_sign = "\n".join([method, content_hash, "", path])
    message = TUYA_CLIENT_ID + token + ts + str_to_sign
    sign = hmac.new(
        TUYA_CLIENT_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest().upper()
    return ts, sign

def get_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    path = "/v1.0/token?grant_type=1"
    ts, sign = _sign("GET", path)
    headers = {
        "client_id":  TUYA_CLIENT_ID,
        "sign":        sign,
        "t":           ts,
        "sign_method": "HMAC-SHA256",
    }
    r = requests.get(TUYA_BASE_URL + path, headers=headers, timeout=10)
    r.raise_for_status()
    data = r.json()["result"]
    _token_cache["token"]      = data["access_token"]
    _token_cache["expires_at"] = now + data["expire_time"] - 60
    return _token_cache["token"]

def get_device_status():
    token = get_token()
    path  = f"/v1.0/devices/{TUYA_DEVICE_ID}/status"
    ts, sign = _sign("GET", path, token=token)
    headers = {
        "client_id":    TUYA_CLIENT_ID,
        "access_token": token,
        "sign":          sign,
        "t":             ts,
        "sign_method":  "HMAC-SHA256",
    }
    r = requests.get(TUYA_BASE_URL + path, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()["result"]

# ── Parsing ZLinky ────────────────────────────────────────────────────────────
# Le ZLinky expose des data points (dp) dont les codes varient selon le firmware.
# Codes les plus courants pour le Lixee ZLinky :
#   SINSTS  / cur_power         → puissance soutirée instantanée (W)
#   SINSTI  / cur_power_inject  → puissance injectée instantanée (W)
# On cherche par code ET par valeur numérique en fallback.

SOUTIRAGE_CODES  = {"SINSTS", "cur_power", "power", "EnergyConsumed"}
INJECTION_CODES  = {"SINSTI", "cur_power_inject", "inject_power", "EnergyProduced"}

def parse_power(status: list) -> dict:
    soutirage = None
    injection = None
    raw = {}

    for dp in status:
        code  = dp.get("code", "")
        value = dp.get("value", 0)
        raw[code] = value
        if code in SOUTIRAGE_CODES:
            soutirage = int(value)
        elif code in INJECTION_CODES:
            injection = int(value)

    # Fallback : si les codes ne correspondent pas, log pour debug
    if soutirage is None or injection is None:
        print(f"[WARN] Codes non reconnus dans : {list(raw.keys())}")
        print(f"[WARN] Valeurs brutes : {raw}")

    return {
        "soutirage_w": soutirage or 0,
        "injection_w": injection or 0,
        "raw":         raw,
    }

# ── Boucle principale ─────────────────────────────────────────────────────────
def main():
    print(f"[INFO] Démarrage — poll toutes les {POLL_INTERVAL}s")
    errors = 0
    while True:
        try:
            status  = get_device_status()
            parsed  = parse_power(status)
            now_utc = datetime.now(timezone.utc).isoformat()

            row = {
                "ts":          now_utc,
                "soutirage_w": parsed["soutirage_w"],
                "injection_w": parsed["injection_w"],
            }

            supabase.table("linky_readings").insert(row).execute()
            print(f"[OK] {now_utc} | soutirage={parsed['soutirage_w']}W | injection={parsed['injection_w']}W")
            errors = 0

        except Exception as e:
            errors += 1
            print(f"[ERR #{errors}] {e}")
            if errors >= 10:
                print("[FATAL] Trop d'erreurs consécutives, arrêt.")
                raise

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
