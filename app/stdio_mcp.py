import sys
import os
import asyncio
import httpx
from dotenv import load_dotenv

# Load env variables
load_dotenv()

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.key_manager import KeyManager
from app.core.video_analysis import VideoAnalysisService
from app.mcp_server import configure_video_service, mcp

async def main():
    # Initialize the key manager (loads GEMINI_KEYS from env)
    key_pool = KeyManager()
    
    # Create the HTTP client
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(180.0),
        limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
    ) as http_client:
        
        # Initialize video analysis service
        video_service = VideoAnalysisService(key_pool, http_client)
        configure_video_service(video_service)
        
        # Run FastMCP in standard input/output transport loop
        # FastMCP.run_stdio_async() is an async function
        await mcp.run_stdio_async()
        
        # Shutdown service
        await video_service.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
