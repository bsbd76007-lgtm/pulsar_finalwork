"""Keep the free Render web instance awake by pinging its own /health endpoint."""
import os
import time
import urllib.request

URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

while URL:
    time.sleep(600)
    try:
        urllib.request.urlopen(URL + "/health", timeout=30).read()
    except Exception as exc:
        print("keepalive ping failed:", exc, flush=True)
