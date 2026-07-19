import requests
import time
import json
import os
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "http://127.0.0.1:8000"
MODEL = os.getenv("GEMINI_DEFAULT_MODEL", "gemini-2.5-flash")
TOKEN = os.getenv("PROXY_API_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}

def test_rotation():
    print("--- Starting 12-Request Stress Test (3 full rotations) ---")
    for i in range(12):
        payload = {
            "model": MODEL,
            "messages": [{"role": "user", "content": f"Stress test ping {i+1}"}],
            "stream": False
        }
        try:
            print(f"[{i+1}/12]", end=" ", flush=True)
            start = time.time()
            resp = requests.post(
                f"{BASE_URL}/v1/chat/completions",
                json=payload,
                headers=HEADERS,
                timeout=60,
            )
            elapsed = time.time() - start
            if resp.status_code == 200:
                print(f"[OK] Success ({elapsed:.1f}s)")
            else:
                print(f"[FAIL] Failed: {resp.status_code} - {resp.text[:100]}")
        except Exception as e:
            print(f"[ERROR] Error: {e}")
        time.sleep(1)

def check_status():
    print("\n--- Verifying Dashboard Stats ---")
    try:
        resp = requests.get(f"{BASE_URL}/v1/status")
        if resp.status_code == 200:
            data = resp.json()
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            print(f"[FAIL] Failed to get status: {resp.status_code}")
    except Exception as e:
        print(f"[ERROR] Status check error: {e}")

if __name__ == "__main__":
    test_rotation()
    check_status()
