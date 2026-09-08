"""
Studio Inspector: Launches visible Chrome with native Profile 1, navigates to Google AI Studio
rate-limit dashboard, waits for chart/data render, and captures a high-resolution snapshot for calibration.
"""

import os
import sys
import time
import subprocess
from pathlib import Path
from typing import Optional
import requests
from playwright.sync_api import sync_playwright

DEFAULT_AI_STUDIO_URL = "https://aistudio.google.com/rate-limit?timeRange=last-1-day&project=gen-lang-client-0386075480"
DEFAULT_PROFILE = "Profile 1"
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
USER_DATA_DIR = Path(os.path.expanduser(r"~\AppData\Local\Google\Chrome\User Data"))
SNAPSHOTS_DIR = Path(__file__).resolve().parent.parent.parent / "logs" / "snapshots"


def find_system_chrome() -> str:
    candidates = [
        CHROME_PATH,
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return CHROME_PATH


class StudioInspector:
    def __init__(
        self,
        profile_name: str = DEFAULT_PROFILE,
        target_url: str = DEFAULT_AI_STUDIO_URL,
        port: int = 9222,
    ):
        self.profile_name = profile_name
        self.target_url = target_url
        self.port = port
        self.chrome_proc: Optional[subprocess.Popen] = None

    def is_port_active(self) -> bool:
        try:
            r = requests.get(f"http://127.0.0.1:{self.port}/json/version", timeout=1.0)
            return r.status_code == 200
        except Exception:
            return False

    def launch_visible_chrome(self) -> bool:
        """Launch Chrome with native User Data and selected Profile with CDP enabled."""
        if self.is_port_active():
            print(f"[*] Chrome already active with debugging port {self.port}.")
            return True

        chrome_exe = find_system_chrome()
        if not os.path.exists(chrome_exe):
            raise FileNotFoundError(f"Chrome executable not found: {chrome_exe}")

        cmd = [
            chrome_exe,
            f"--user-data-dir={USER_DATA_DIR}",
            f"--profile-directory={self.profile_name}",
            f"--remote-debugging-port={self.port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            self.target_url,
        ]

        print(f"\n[*] Launching visible native Chrome:")
        print(f"    Profile:   {self.profile_name}")
        print(f"    Target:    {self.target_url}")
        print(f"    CDP Port:  {self.port}")

        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self.chrome_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

        # Wait for CDP endpoint to respond
        for _ in range(15):
            time.sleep(1)
            if self.is_port_active():
                print(f"[✓] Chrome launched successfully and CDP responding on port {self.port}!")
                return True

        print("[!] Chrome started but CDP port not ready yet, will attempt connection...")
        return False

    def capture_snapshot(self, output_path: Optional[str] = None, wait_seconds: int = 8) -> str:
        """Connect to visible Chrome via CDP, wait for chart to render, and take screenshot."""
        self.launch_visible_chrome()

        out_dir = SNAPSHOTS_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = f"ai_studio_quota_{time.strftime('%Y%m%d_%H%M%S')}.png"
        target_file = Path(output_path) if output_path else (out_dir / filename)

        print(f"\n[*] Connecting Playwright to visible Chrome (CDP port {self.port})...")
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}")
            contexts = browser.contexts
            ctx = contexts[0] if contexts else browser.new_context()

            # Find or navigate to AI Studio tab
            page = None
            for p_item in ctx.pages:
                if "aistudio.google.com" in p_item.url:
                    page = p_item
                    break
            if not page:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(self.target_url, wait_until="domcontentloaded")

            print(f"[*] Active Page Title: {page.title()}")
            print(f"[*] Waiting {wait_seconds}s for rate-limit charts and quota metrics to render...")
            time.sleep(wait_seconds)

            # Take full view snapshot
            page.screenshot(path=str(target_file), full_page=False)
            print(f"[✓] AI Studio Quota snapshot saved successfully to:")
            print(f"    {target_file.resolve()}\n")
            return str(target_file.resolve())
