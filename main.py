import os

import uvicorn
from dotenv import load_dotenv

# Load env before anything else
load_dotenv()

if __name__ == "__main__":
    host = os.getenv("PROXY_HOST", "127.0.0.1")
    port = int(os.getenv("PROXY_PORT", "8000"))
    reload_enabled = os.getenv("PROXY_RELOAD", "false").lower() == "true"
    display_host = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host

    print("Starting Gemini Proxy Pool...")
    print(f"  Dashboard: http://{display_host}:{port}")
    print(f"  API docs:  http://{display_host}:{port}/docs")
    print(f"  Bind:      {host}:{port}")
    print()
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload_enabled,
    )
