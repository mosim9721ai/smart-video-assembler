#!/usr/bin/env python3
"""One-shot CLI: script.txt → MP4 + SRT.

Usage:
  python cli.py samples/holding_your_breath.txt              # → output.mp4
  python cli.py my_script.txt my_video.mp4                    # custom output
  STYLE="dark cinematic, 4k" RATIO=16:9 python cli.py x.txt   # override via env

Requires FAL_KEY in .env (or as env var) and ffmpeg on PATH.
"""

import os
import shutil
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from app import (  # noqa: E402
    DEFAULT_FAL_LLM_MODEL,
    DEFAULT_FAL_LLM_SUBMODEL,
    DEFAULT_FAL_MODEL,
    WORK,
    assemble_video,
    audio_duration,
    fmt_srt_time,
    run,
    split_script,
)

BACKEND = os.environ.get("BACKEND", "fal").lower()
if BACKEND == "free":
    from free_backend import (  # noqa: E402
        generate_one_image,
        generate_visual_prompts,
        resolve_tts,
        tts_one_chunk,
    )
else:
    from app import (  # noqa: E402
        generate_one_image,
        generate_visual_prompts,
        resolve_tts,
        tts_one_chunk,
    )


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0 if len(sys.argv) > 1 else 1)

    script_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("output.mp4")

    style = os.environ.get(
        "STYLE",
        "cinematic, dramatic lighting, film grain, photorealistic, 4k",
    )
    ratio = os.environ.get("RATIO", "9:16")
    fps = int(os.environ.get("FPS", "30"))

    print(f"🔧 Backend: {BACKEND}")
    if BACKEND != "free" and not os.environ.get("FAL_KEY"):
        print("❌ FAL_KEY not set. Copy .env.example to .env and fill it in,")
        print("   OR run with BACKEND=free (no key needed).")
        sys.exit(1)

    if not script_path.exists():
        print(f"❌ Script not found: {script_path}")
        sys.exit(1)

    script = script_path.read_text(encoding="utf-8-sig")
    print(f"📖 Script: {script_path} ({len(script)} chars)")

    chunks = split_script(script)
    print(f"✂️  Chunks: {len(chunks)}")
    if not chunks:
        print("❌ Script has no usable chunks.")
        sys.exit(1)

    tts_model, voice, lang = resolve_tts(script, "auto", "auto")
    print(f"🌐 Language={lang}  TTS={tts_model} voice={voice}")

    print("💭 Generating visual prompts (1 LLM call)…")
    prompts = generate_visual_prompts(
        chunks, style, DEFAULT_FAL_LLM_MODEL, DEFAULT_FAL_LLM_SUBMODEL
    )
    print(f"   → {len(prompts)} prompts")

    job_id = uuid.uuid4().hex[:8]
    job = WORK / job_id
    job.mkdir(parents=True, exist_ok=True)
    audio_dir = job / "audio_chunks"
    audio_dir.mkdir()
    image_dir = job / "images"
    image_dir.mkdir()

    total = len(chunks)
    done = {"tts": 0, "img": 0}

    def do_tts(i, text):
        data = tts_one_chunk(text, tts_model, voice)
        (audio_dir / f"{i:04d}.mp3").write_bytes(data)
        done["tts"] += 1
        print(f"   🔊 audio {done['tts']}/{total}")

    def do_image(i, prompt):
        data = generate_one_image(DEFAULT_FAL_MODEL, prompt, ratio)
        (image_dir / f"{i:04d}.jpg").write_bytes(data)
        done["img"] += 1
        print(f"   🖼️  image {done['img']}/{total}")

    print(f"🎨 Fal.ai — {total} images + {total} audio (4 workers)…")
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = []
        for i, c in enumerate(chunks):
            futs.append(ex.submit(do_tts, i, c))
        for i, p in enumerate(prompts):
            futs.append(ex.submit(do_image, i, p))
        for f in as_completed(futs):
            f.result()

    audio_paths = [audio_dir / f"{i:04d}.mp3" for i in range(total)]
    image_paths = [image_dir / f"{i:04d}.jpg" for i in range(total)]

    cues = []
    t = 0.0
    for i, ap in enumerate(audio_paths):
        d = max(0.4, audio_duration(ap))
        cues.append({"start": t, "end": t + d, "text": chunks[i]})
        t += d
    print(f"⏱️  Total duration: {t:.1f}s")

    srt = job / "captions.srt"
    with open(srt, "w", encoding="utf-8") as f:
        for i, c in enumerate(cues):
            f.write(
                f"{i+1}\n"
                f"{fmt_srt_time(c['start'])} --> {fmt_srt_time(c['end'])}\n"
                f"{c['text']}\n\n"
            )

    print("🎧 Concatenating audio…")
    concat = job / "audio_concat.txt"
    with open(concat, "w") as f:
        for ap in audio_paths:
            f.write(f"file '{ap.as_posix()}'\n")
    voice_mp3 = job / "voice.mp3"
    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat),
        "-c:a", "libmp3lame", "-b:a", "128k",
        str(voice_mp3),
    ])

    print("🎬 Assembling video…")
    output, w, h = assemble_video(job, voice_mp3, cues, image_paths, ratio, fps)

    shutil.copy(output, out_path)
    shutil.copy(srt, out_path.with_suffix(".srt"))
    print()
    print(f"✅ Done!")
    print(f"   📺 {out_path.resolve()}  ({w}x{h}, {t:.1f}s)")
    print(f"   📝 {out_path.with_suffix('.srt').resolve()}")


if __name__ == "__main__":
    main()
