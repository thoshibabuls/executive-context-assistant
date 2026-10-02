"""Check if GEMINI_API_KEY works. Reads .env (cwd or script dir) then env vars.

Run: python test_gemini_key.py
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_env():
    for base in (Path.cwd(), Path(__file__).parent):
        f = next((base / n for n in (".env", "env") if (base / n).is_file()), base / ".env")
        if f.is_file():
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return


def call(url, payload=None):
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "x-goog-api-key": os.environ.get("GEMINI_API_KEY", "")})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main():
    load_env()
    key = os.environ.get("GEMINI_API_KEY")
    model = os.environ.get("API_GEMINI_MODEL", "gemini-2.5-flash")
    if not key:
        sys.exit("FAIL: GEMINI_API_KEY not set")

    base = "https://generativelanguage.googleapis.com/v1beta"

    # 1. key validity: list models
    status, body = call(f"{base}/models")
    if status != 200:
        sys.exit(f"FAIL key check: HTTP {status} {body.get('error', {}).get('message')}")
    names = [m["name"].removeprefix("models/") for m in body.get("models", [])]
    print(f"OK key valid. {len(names)} models visible.")
    print(f"Model '{model}' available: {model in names}")

    # 2. real generation call
    status, body = call(
        f"{base}/models/{model}:generateContent",
        {"contents": [{"parts": [{"text": "Reply with the single word: pong"}]}]},
    )
    if status != 200:
        sys.exit(f"FAIL generate: HTTP {status} {body.get('error', {}).get('message')}")
    text = body["candidates"][0]["content"]["parts"][0]["text"]
    print(f"OK generate. Model said: {text.strip()}")


if __name__ == "__main__":
    main()
