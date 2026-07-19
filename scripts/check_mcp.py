import asyncio
import argparse
import json
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(url: str, smoke: bool = False, source: str | None = None, job_id: str | None = None, output: str | None = None):
    async with streamable_http_client(url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            for tool in tools.tools:
                print(tool.name)
            if smoke:
                result = await session.call_tool(
                    "submit_video_analysis",
                    {"source": "https://example.com/not-supported.mp4"},
                )
                print(result.content[0].text)
            if source:
                result = await session.call_tool(
                    "submit_video_analysis",
                    {"source": source, "analysis_type": "remotion"},
                )
                print(result.content[0].text)
            if job_id:
                result = await session.call_tool(
                    "get_video_analysis", {"job_id": job_id}
                )
                result_text = result.content[0].text
                payload = json.loads(result_text)
                if output:
                    destination = Path(output)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(result_text, encoding="utf-8")
                summary = payload.get("result") or {}
                print(
                    json.dumps(
                        {
                            "id": payload.get("id"),
                            "status": payload.get("status"),
                            "progress": payload.get("progress"),
                            "summary": summary.get("summary"),
                            "timeline_count": len(summary.get("timeline", [])),
                            "output": str(Path(output).resolve()) if output else None,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp/")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--submit")
    parser.add_argument("--job")
    parser.add_argument("--output")
    args = parser.parse_args()
    asyncio.run(
        main(
            args.url,
            smoke=args.smoke,
            source=args.submit,
            job_id=args.job,
            output=args.output,
        )
    )
