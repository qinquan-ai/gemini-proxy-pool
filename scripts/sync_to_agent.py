"""
Sync current Antigravity IDE conversation and workspaces to Antigravity Agent app (non-IDE).
Enables seamless mobile Remote Control of this session under Project 'TikTok'.
"""

import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CONVERSATION_ID = "21bf14c5-31aa-413d-90a3-11aa3d8ee09e"
IDE_ROOT = Path(r"C:\Users\Administrator\.gemini\antigravity-ide")
AGENT_ROOT = Path(r"C:\Users\Administrator\.gemini\antigravity")

def sync():
    print(f"[*] Starting session migration for: {CONVERSATION_ID}")

    # 1. SQLite online backup
    src_db = IDE_ROOT / "conversations" / f"{CONVERSATION_ID}.db"
    dst_db = AGENT_ROOT / "conversations" / f"{CONVERSATION_ID}.db"
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    
    if src_db.exists():
        src_conn = sqlite3.connect(str(src_db))
        dst_conn = sqlite3.connect(str(dst_db))
        src_conn.backup(dst_conn)
        dst_conn.close()
        src_conn.close()
        print(f"[✓] Conversation SQLite database synchronized: {dst_db}")

    # 2. Mirror Brain Directory (transcripts, artifacts, logs)
    src_brain = IDE_ROOT / "brain" / CONVERSATION_ID
    dst_brain = AGENT_ROOT / "brain" / CONVERSATION_ID
    if src_brain.exists():
        dst_brain.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_brain, dst_brain, dirs_exist_ok=True)
        print(f"[✓] Brain artifacts and transcripts synchronized: {dst_brain}")

    # 3. Write session annotation
    ann_file = AGENT_ROOT / "annotations" / f"{CONVERSATION_ID}.pbtxt"
    ann_file.parent.mkdir(parents=True, exist_ok=True)
    now_sec = int(time.time())
    ann_file.write_text(f'title: "TikTok and StudioKey Automation" last_user_view_time: {{seconds: {now_sec} nanos: 0}}\n', encoding="utf-8")
    print(f"[✓] Session annotation created: {ann_file}")

    print("\n[✓] Session successfully imported into Antigravity Agent app!")

if __name__ == "__main__":
    sync()
