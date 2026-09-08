"""
StudioKey CLI: Key and Model Quota Management
Usage:
    python -m app.cli quota
    python -m app.cli history
    python -m app.cli recommend
"""

import sys
import os
import argparse
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# Ensure utf-8 output on Windows console
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Load .env file
load_dotenv(PROJECT_ROOT / ".env")

from app.core.key_manager import KeyManager, DEFAULT_MODEL_RPD


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    """Format tabular data into a neat ASCII table."""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for idx, val in enumerate(row):
            col_widths[idx] = max(col_widths[idx], len(str(val)))

    sep = "+-" + "-+-".join("-" * w for w in col_widths) + "-+"
    header_line = "| " + " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers)) + " |"

    lines = [sep, header_line, sep]
    for row in rows:
        row_line = "| " + " | ".join(str(val).ljust(col_widths[i]) for i, val in enumerate(row)) + " |"
        lines.append(row_line)
    lines.append(sep)
    return "\n".join(lines)


def cmd_quota(args):
    km = KeyManager()
    status = km.get_quota_status()

    print("\n" + "=" * 68)
    print(" [*] StudioKey Quota Status (Gemini Pacific Time PT)")
    print("=" * 68)
    print(f"  Pacific Date:     {status['current_pacific_date']}")
    print(f"  Next Reset In:    {status['next_reset_in_hours']} hours ({status['seconds_to_next_reset']}s)")
    print(f"  Total Keys:       {len(status['keys'])}")
    print("=" * 68 + "\n")

    # 1. Summary by Model
    print("[+] Model Quota Summary Across All Keys:")
    headers = ["Model", "RPD Limit", "Used Today", "Remaining", "Active Keys", "Usage %"]
    rows = []
    for model, info in status.get("summary_by_model", {}).items():
        used = info["total_used"]
        limit = info["total_limit"]
        rem = info["total_remaining"]
        active = info["active_keys"]
        pct = f"{(used / limit * 100):.1f}%" if limit > 0 else "0.0%"
        rows.append([model, str(limit), str(used), str(rem), str(active), pct])
    print(format_table(headers, rows))

    # 2. Key Details
    print("\n[+] Individual Key Quotas:")
    k_headers = ["Key Name", "Key Prefix", "Status", "Model", "Used/Limit", "Remaining", "Cooldown"]
    k_rows = []
    for k in status.get("keys", []):
        name = k["name"]
        prefix = k["key_prefix"]
        k_status = "Disabled" if k["disabled"] else "Enabled"
        first = True
        for m_name, m_info in k["models"].items():
            used_str = f"{m_info['used_today']}/{m_info['limit_rpd']}"
            rem_str = str(m_info["remaining"])
            m_stat = m_info["status"]
            cd = f"{m_info['cooldown_remaining_sec']}s" if m_info['cooldown_remaining_sec'] > 0 else "-"
            if first:
                k_rows.append([name, prefix, k_status, m_name, used_str, rem_str, cd])
                first = False
            else:
                k_rows.append(["", "", "", m_name, used_str, rem_str, cd])
    if k_rows:
        print(format_table(k_headers, k_rows))
    else:
        print(" (No active keys found in environment)")
    print()


def cmd_history(args):
    km = KeyManager()
    status = km.get_quota_status()
    history = status.get("history", {})

    print("\n" + "=" * 68)
    print(" [*] StudioKey Daily Quota Consumption History")
    print("=" * 68)

    if not history:
        print(" (No historical usage records found yet)\n")
        return

    headers = ["Date (PT)", "Model", "Requests Count"]
    rows = []
    for dt in sorted(history.keys(), reverse=True):
        date_data = history[dt]
        first = True
        for model, cnt in sorted(date_data.items()):
            if first:
                rows.append([dt, model, str(cnt)])
                first = False
            else:
                rows.append(["", model, str(cnt)])
    print(format_table(headers, rows))
    print()


def cmd_recommend(args):
    km = KeyManager()
    status = km.get_quota_status()
    summary = status.get("summary_by_model", {})

    print("\n" + "=" * 68)
    print(" [*] Smart Model Recommendation")
    print("=" * 68)

    if not summary:
        print(" (No models available)\n")
        return

    # Sort models by remaining quota descending
    ranked = sorted(
        summary.items(),
        key=lambda item: item[1]["total_remaining"],
        reverse=True
    )

    print("\nBased on current remaining quota across all keys:")
    for rank, (model, info) in enumerate(ranked, 1):
        rem = info["total_remaining"]
        limit = info["total_limit"]
        print(f"  {rank}. {model:<26} -> Remaining: {rem:>4}/{limit:<4} calls")

    best_model, best_info = ranked[0]
    if best_info["total_remaining"] > 0:
        print(f"\n-> Recommended choice: {best_model} (most remaining quota)")
    else:
        print("\n[!] All models appear to have exhausted their daily quota!")
    print()


def cmd_snapshot(args):
    from app.core.studio_inspector import StudioInspector, DEFAULT_AI_STUDIO_URL, DEFAULT_PROFILE
    inspector = StudioInspector(
        profile_name=args.profile or DEFAULT_PROFILE,
        target_url=args.url or DEFAULT_AI_STUDIO_URL,
    )
    saved_img = inspector.capture_snapshot(
        output_path=args.output,
        wait_seconds=args.wait,
        headless=getattr(args, "headless", False),
    )
    print(f"[*] Snapshot ready for quota calibration: {saved_img}")


def cmd_batch(args):
    import asyncio
    from app.core.parallel_analyzer import ParallelVideoAnalyzer
    
    analyzer = ParallelVideoAnalyzer(
        concurrency_per_key=args.concurrency,
        inter_request_delay=args.delay,
    )
    video_files = []
    if args.dir:
        v_dir = Path(args.dir)
        if v_dir.exists():
            video_files = [str(p) for p in v_dir.glob("*.mp4")]
    if args.files:
        video_files.extend(args.files)

    if not video_files:
        print("[!] 未找到可分析的视频文件。请通过 --dir 或 --files 指定。")
        return

    asyncio.run(analyzer.run_parallel_batch(
        video_files=video_files,
        analysis_type=args.type,
        output_dir=args.output,
    ))



def cmd_download(args):
    from app.core.video_downloader import VideoDownloader, VideoDownloadError

    sources = args.sources
    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    downloader = VideoDownloader(
        cookies_from_browser=args.cookies_from_browser or "",
    )

    print("\n" + "=" * 68)
    print(" [*] StudioKey Universal Video Downloader")
    print(f"  Target Directory: {output_dir}")
    print(f"  Total Videos:     {len(sources)}")
    print("=" * 68 + "\n")

    success_count = 0
    for idx, source in enumerate(sources, 1):
        print(f"[{idx}/{len(sources)}] Processing: {source}")
        try:
            saved_file, meta = downloader.download(
                source=source,
                output_dir=output_dir,
                filename=args.filename if len(sources) == 1 else None,
            )
            size_mb = saved_file.stat().st_size / (1024 * 1024)
            dur = meta.get("duration") or "N/A"
            title = meta.get("title") or saved_file.stem
            print(f"  [✓] Successfully downloaded: {saved_file.name}")
            print(f"      Size: {size_mb:.2f} MB | Duration: {dur}s | Extractor: {meta.get('extractor', 'default')}\n")
            success_count += 1
        except Exception as e:
            print(f"  [!] Failed: {e}\n")

    print(f"[*] Finished: {success_count}/{len(sources)} downloaded successfully.\n")


def main():
    parser = argparse.ArgumentParser(description="StudioKey Proxy CLI Management Tool")
    subparsers = parser.add_subparsers(dest="subcommand", help="Available commands")

    p_download = subparsers.add_parser("download", help="Download videos from Douyin/TikTok, YouTube, Bilibili, or web URLs")
    p_download.add_argument("sources", nargs="+", help="One or more video URLs or share text")
    p_download.add_argument("--output", "-o", default="downloads", help="Output directory (default: ./downloads)")
    p_download.add_argument("--filename", "-f", help="Custom output filename (only for single video download)")
    p_download.add_argument("--cookies-from-browser", default="", help="Load cookies from browser (e.g., chrome, edge)")
    p_download.set_defaults(func=cmd_download)

    p_quota = subparsers.add_parser("quota", help="Display current quota and reset countdown")
    p_quota.set_defaults(func=cmd_quota)

    p_history = subparsers.add_parser("history", help="Display 30-day quota history")
    p_history.set_defaults(func=cmd_history)

    p_recommend = subparsers.add_parser("recommend", help="Recommend optimal model based on remaining quota")
    p_recommend.set_defaults(func=cmd_recommend)

    p_snap = subparsers.add_parser("snapshot", help="Capture AI Studio quota snapshot & metrics (default: visible mode; --headless for silent background)")
    p_snap.add_argument("--headless", action="store_true", help="Run in background headless mode without opening visible browser window")
    p_snap.add_argument("--profile", default="0", help="Chrome profile index (0, 1) or directory name (Default, Profile 1), default: 0")
    p_snap.add_argument("--url", help="AI Studio rate limit URL")
    p_snap.add_argument("--wait", type=int, default=8, help="Wait seconds for chart rendering")
    p_snap.add_argument("--output", "-o", help="Custom output image file path")
    p_snap.set_defaults(func=cmd_snapshot)

    p_batch = subparsers.add_parser("batch", help="Run multi-account parallel video analysis")
    p_batch.add_argument("--dir", help="Directory containing .mp4 video files")
    p_batch.add_argument("--files", nargs="+", help="Explicit list of video files")
    p_batch.add_argument("--concurrency", "-c", type=int, default=3, help="Max in-flight concurrency per account (default: 3, total 9 across 3 accounts)")
    p_batch.add_argument("--delay", type=float, default=2.0, help="Inter-request delay in seconds to safeguard RPM (default: 2.0)")
    p_batch.add_argument("--type", default="curation", help="Analysis type (curation, transcript, etc.)")
    p_batch.add_argument("--output", "-o", help="Output directory for generated markdown reports")
    p_batch.set_defaults(func=cmd_batch)

    args = parser.parse_args()
    if not args.subcommand:
        cmd_quota(args)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
