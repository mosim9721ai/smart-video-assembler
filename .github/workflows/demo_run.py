"""Script executed by the demo-video workflow.

Builds a small SRT, generates one Pollinations image per cue, and
assembles a Story MP4 using the same helpers the Flask server uses.
Outputs land in ./demo_out for actions/upload-artifact to pick up.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import assemble_from_images, parse_srt  # noqa: E402
from pollinations_images import generate_for_cues  # noqa: E402

STYLE = os.environ.get("STYLE", "cinematic")
MODEL = os.environ.get("MODEL", "flux")
RATIO = os.environ.get("RATIO", "9:16")
EXTRA = os.environ.get("EXTRA", "").strip() or None

OUT = Path("demo_out")
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir()

srt = """1
00:00:00,000 --> 00:00:03,000
Sunrise over Himalayan mountains, prayer flags fluttering in the wind

2
00:00:03,000 --> 00:00:06,000
A small Indian village waking up, chai stalls opening

3
00:00:06,000 --> 00:00:09,000
An old farmer walking through green terraced fields with his dog

4
00:00:09,000 --> 00:00:12,000
Golden sunset with orange sky over the same mountains
"""
srt_path = OUT / "captions.srt"
srt_path.write_text(srt)
cues = parse_srt(srt)
print(f"Parsed {len(cues)} cues")

if RATIO == "9:16":
    w, h = 720, 1280
elif RATIO == "16:9":
    w, h = 1280, 720
else:
    w, h = 1024, 1024

img_dir = OUT / "images"
img_dir.mkdir()

def progress(i, text):
    print(f"[{i}/{len(cues)}] {text[:70]}", flush=True)

paths = generate_for_cues(
    cues, out_dir=img_dir, style=STYLE, extra=EXTRA, model=MODEL,
    width=w, height=h, on_progress=progress,
)
print(f"Generated {len(paths)} images")

# Copy for easier artifact browsing.
for p in paths:
    shutil.copy(p, OUT / p.name)

audio = OUT / "audio.mp3"
subprocess.run([
    "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
    "-t", "12", "-c:a", "libmp3lame", "-b:a", "128k", str(audio),
], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

work = OUT / "assemble"
work.mkdir()
output, meta = assemble_from_images(work, cues, paths, audio, RATIO, fps=30)
final = OUT / "story.mp4"
shutil.copy(output, final)
print(f"Wrote {final}  ({final.stat().st_size} bytes)")
print("Meta:", meta)
