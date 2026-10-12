import os
import argparse
import uvicorn
from dotenv import load_dotenv

# Load env before anything else
load_dotenv()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start StudioKey Gemini Proxy Gateway")
    parser.add_argument("--port", "-p", type=int, default=None, help="Port to bind (overrides PROXY_PORT in .env)")
    parser.add_argument("--host", "-H", type=str, default=None, help="Host to bind (overrides PROXY_HOST in .env)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload on code changes")
    args = parser.parse_args()

    host = args.host or os.getenv("PROXY_HOST", "127.0.0.1")
    port = args.port or int(os.getenv("PROXY_PORT", "8008"))
    reload_enabled = args.reload or (os.getenv("PROXY_RELOAD", "false").lower() == "true")
    display_host = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host

    print("Starting StudioKey Proxy Gateway...")
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
