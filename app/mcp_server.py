import asyncio

from mcp.server.fastmcp import Context, FastMCP

from app.core.video_analysis import VideoAnalysisError, VideoAnalysisService


mcp = FastMCP(
    "StudioKey Video Analysis",
    instructions=(
        "Analyze public YouTube, Douyin, Bilibili links, pasted share text, and "
        "local video files with the shared StudioKey Gemini pool. "
        "Default mode 'general' provides objective audiovisual and narrative analysis. "
        "Modes: 'general' (default content analysis), 'curation' (short drama/video repurposing and watermark scoring), "
        "'remotion' (Remotion React component plan), 'vox', 'vlog', 'technical', 'transcript'."
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
    analysis_type: str = "general",
    prompt: str | None = None,
    model: str = "gemini-3.5-flash-lite",
    archive_dir: str | None = None,
    save_threshold: float = 7.0,
) -> dict:
    """Submit YouTube/Douyin/Bilibili share text, a URL, or a local file for analysis.
    
    Args:
        source: Video URL, share text, or local file path.
        analysis_type: Analysis mode ('general', 'curation', 'remotion', 'vox', 'vlog', 'technical', 'transcript').
        prompt: Optional custom instructions or focus points.
        model: Gemini model to use for multimodal processing.
        archive_dir: Optional directory to archive high-quality video & report (zero disk usage if omitted).
        save_threshold: Minimum curation_score (default 7.0) to trigger auto-archiving.
    """
    try:
        return await get_video_service().submit(
            source=source,
            analysis_type=analysis_type,
            prompt=prompt,
            model=model,
            archive_dir=archive_dir,
            save_threshold=save_threshold,
        )
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
    analysis_type: str = "general",
    prompt: str | None = None,
    model: str = "gemini-3.5-flash-lite",
    archive_dir: str | None = None,
    save_threshold: float = 7.0,
    timeout_seconds: float = 900,
    ctx: Context | None = None,
) -> dict:
    """Analyze a video synchronously while reporting stage progress.
    
    Args:
        source: Video URL, share text, or local file path.
        analysis_type: Analysis mode ('general', 'curation', 'remotion', 'vox', 'vlog', 'technical', 'transcript').
        prompt: Optional custom instructions or focus points.
        model: Gemini model to use.
        archive_dir: Optional directory to archive high-quality video & report.
        save_threshold: Minimum curation score (default 7.0) to auto-archive.
        timeout_seconds: Max seconds to wait for completion.
    """
    submitted = await submit_video_analysis(
        source=source,
        analysis_type=analysis_type,
        prompt=prompt,
        model=model,
        archive_dir=archive_dir,
        save_threshold=save_threshold,
    )
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


@mcp.tool()
async def generate_image(
    prompt: str,
    image_name: str = "generated_image",
    aspect_ratio: str = "1:1",
    output_path: str | None = None,
    image_paths: list[str] | str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Generate high-quality images or edit based on reference images using Google Imagen 4.0 via AGY engine.

    Args:
        prompt: Detailed visual description or editing instructions.
        image_name: Short identifier name for the image (lowercase_with_underscores).
        aspect_ratio: Aspect ratio of the generated image. Supported values: '1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3'. Default is '1:1'.
        output_path: Optional local destination file path to save the generated image.
        image_paths: Optional list of reference/input image paths (max 3) for image-to-image, style transfer, or editing.
    """
    from app.core.agy_image_generator import generate_image_async

    if ctx:
        await ctx.info(f"Generating image with prompt: {prompt[:60]}...")
    return await generate_image_async(
        prompt=prompt,
        image_name=image_name,
        aspect_ratio=aspect_ratio,
        output_path=output_path,
        image_paths=image_paths,
    )


@mcp.tool()
async def analyze_image(
    image_path: list[str] | str,
    prompt: str = "请详细分析并描述这张图片的内容。",
    ctx: Context | None = None,
) -> dict:
    """Analyze local image(s) using Antigravity (AGY) multimodal vision engine.
    Supports UI component breakdown, design extraction, OCR text recognition, bug/error screenshot diagnosis, and visual question answering.

    Args:
        image_path: Absolute or relative local path to the image file (or list of paths). Supported: JPG, PNG, WEBP, GIF, BMP.
        prompt: Analysis requirements (e.g., 'Break down this UI layout into React components', 'Extract all text from this screenshot', 'Diagnose what caused this error').
    """
    from app.core.agy_image_analyzer import analyze_image_async

    path_display = image_path if isinstance(image_path, str) else ", ".join(image_path[:2])
    if ctx:
        await ctx.info(f"Analyzing image ({path_display}) with AGY engine...")
    return await analyze_image_async(
        image_path=image_path,
        prompt=prompt,
    )
