from pathlib import Path
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

root = Path(r"C:\Users\Administrator\.gemini\antigravity")
pb = (root / "agyhub_summaries_proto.pb").read_bytes()

# Find all UUIDs (36 chars)
uuids = re.findall(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", pb)
seen = set()
for u in uuids:
    us = u.decode()
    if us in seen:
        continue
    seen.add(us)
    idx = pb.find(u)
    context = pb[max(0, idx-30):min(len(pb), idx+100)]
    # Look for readable title
    titles = re.findall(rb"[A-Za-z0-9_\- ]{4,40}", context)
    readable = [t.decode(errors="ignore") for t in titles if len(t) > 3]
    print(f"UUID: {us} -> Context readable: {readable[:3]}")
