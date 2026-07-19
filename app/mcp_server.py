import asyncio

from mcp.server.fastmcp import Context, FastMCP

from app.core.video_analysis import VideoAnalysisError, VideoAnalysisService


mcp = FastMCP(
    "StudioKey Video Analysis",
    instructions=(
        "Analyze public YouTube or Douyin links, pasted Douyin share text, and "
        "local files with the shared StudioKey Gemini pool. Return structured "
        "Remotion-ready visual and audio evidence."
    ),
    stateless_http=True,
    json_response=True,
)
mcp.settings.streamable_http_path = "/"
_video_service: VideoAnalysisService | None = None


def configure_video_service(service: VideoAnalysisService):
    global _video_service
    _video_service = service


def get_video_service() -> VideoAnalysisService:
    if _video_service is None:
        raise RuntimeError("Video analysis service is not initialized")
    return _video_service


@mcp.tool()
async def submit_video_analysis(
    source: str,
    analysis_type: str = "remotion",
    prompt: str | None = None,
    model: str = "gemini-3-flash-preview",
) -> dict:
    """Submit YouTube/Douyin share text, a URL, or a local file for analysis."""
    try:
        return await get_video_service().submit(source, analysis_type, prompt, model)
    except VideoAnalysisError as exc:
        return {"status": "rejected", "error": str(exc)}


@mcp.tool()
def get_video_analysis(job_id: str) -> dict:
    """Get progress or the structured result of a video analysis job."""
    try:
        return get_video_service().get(job_id)
    except VideoAnalysisError as exc:
        return {"status": "not_found", "error": str(exc)}


@mcp.tool()
async def cancel_video_analysis(job_id: str) -> dict:
    """Cancel a queued or running video analysis job."""
    try:
        return await get_video_service().cancel(job_id)
    except VideoAnalysisError as exc:
        return {"status": "not_found", "error": str(exc)}


@mcp.tool()
async def analyze_video(
    source: str,
    analysis_type: str = "remotion",
    prompt: str | None = None,
    model: str = "gemini-3-flash-preview",
    timeout_seconds: float = 900,
    ctx: Context | None = None,
) -> dict:
    """Analyze a video synchronously while reporting stage progress."""
    submitted = await submit_video_analysis(source, analysis_type, prompt, model)
    if submitted.get("status") == "rejected":
        return submitted
    job_id = submitted["id"]
    last_stage = None
    deadline = asyncio.get_running_loop().time() + max(0, timeout_seconds)
    while True:
        job = get_video_service().get(job_id)
        if ctx and job["stage"] != last_stage:
            await ctx.info(f"Video job {job_id}: {job['stage']}")
            await ctx.report_progress(
                progress=job["progress"], total=1.0, message=job["stage"]
            )
            last_stage = job["stage"]
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        if timeout_seconds <= 0 or asyncio.get_running_loop().time() >= deadline:
            return {**job, "wait_timed_out": True}
        await asyncio.sleep(1)


@mcp.resource("studiokey://video-analysis/capabilities")
def video_analysis_capabilities() -> str:
    """Describe accepted sources and analysis modes."""
    return (
        "Sources: local supported video files, public YouTube URLs, Douyin URLs "
        "or complete pasted Douyin share text, Gemini Files API URIs, and gs:// "
        "URIs. Modes: remotion, vox, vlog, technical, transcript, general. "
        "Background jobs and completed results are persisted."
    )
