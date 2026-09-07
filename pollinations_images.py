"""
Generate images with Pollinations.ai — 100% free, no API key, no signup.

Pollinations exposes an HTTP GET endpoint that returns a JPEG for a given
prompt. Under the hood it runs Flux Schnell (and a few other open models).

Public entry points:
    generate_image(prompt, ...) -> bytes (JPEG)
    generate_for_cues(cues, ...) -> list[Path]
"""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable

HOST = "https://image.pollinations.ai"
DEFAULT_MODEL = "flux"
AVAILABLE_MODELS = ("flux", "flux-realism", "flux-anime", "flux-3d",
                    "any-dark", "turbo")

STYLE_PRESETS = {
    "cinematic": (
        "Cinematic still, dramatic lighting, shallow depth of field, film grain, "
        "high detail, movie poster composition"
    ),
    "anime": (
        "Anime style illustration, vibrant colors, clean line art, expressive, "
        "studio-quality"
    ),
    "photorealistic": (
        "Photorealistic, natural lighting, sharp focus, high dynamic range, "
        "shot on 50mm lens"
    ),
    "storybook": (
        "Children's storybook illustration, soft pastel colors, warm lighting, "
        "hand-drawn feel"
    ),
    "3d": (
        "3D rendered scene, octane render, volumetric lighting, high polygon "
        "detail, Pixar style"
    ),
    "watercolor": (
        "Watercolor painting, soft edges, flowing colors, textured paper, "
        "artistic"
    ),
    "comic": (
        "Comic book style, bold ink outlines, halftone shading, dynamic panel "
        "composition"
    ),
    "minimalist": (
        "Minimalist flat illustration, limited color palette, geometric shapes, "
        "clean vector look"
    ),
}


class PollinationsError(RuntimeError):
    pass


def _build_prompt(cue_text: str, style: str = "cinematic",
                  extra: str | None = None) -> str:
    style_hint = STYLE_PRESETS.get(style, STYLE_PRESETS["cinematic"])
    parts = [f"Scene: {cue_text.strip()}", style_hint]
    if extra:
        parts.append(extra.strip())
    parts.append("No text, no captions, no watermarks in the image")
    return ". ".join(parts)


def _seed_from(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


def generate_image(
    prompt: str,
    width: int = 1024,
    height: int = 1024,
    model: str = DEFAULT_MODEL,
    seed: int | None = None,
    timeout: float = 90.0,
    retries: int = 2,
) -> bytes:
    """Fetch one image from Pollinations. Returns raw bytes (JPEG)."""
    if not prompt.strip():
        raise PollinationsError("Empty prompt")
    if seed is None:
        seed = _seed_from(prompt)

    encoded = urllib.parse.quote(prompt, safe="")
    qs = urllib.parse.urlencode({
        "width": width,
        "height": height,
        "model": model,
        "seed": seed,
        "nologo": "true",
        "enhance": "true",
    })
    url = f"{HOST}/prompt/{encoded}?{qs}"

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "smart-video-assembler/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            if not data or len(data) < 512:
                raise PollinationsError(
                    f"Response too small ({len(data)} bytes), likely an error"
                )
            return data
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                last_err = PollinationsError(f"HTTP {e.code}")
                time.sleep(2 ** attempt)
                continue
            raise PollinationsError(f"HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                last_err = PollinationsError(f"Network: {e}")
                time.sleep(2 ** attempt)
                continue
            raise PollinationsError(f"Network: {e}") from None
        except PollinationsError:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise

    raise last_err or PollinationsError("Unknown failure")


def generate_for_cues(
    cues: Iterable[dict],
    out_dir: str | Path,
    style: str = "cinematic",
    extra: str | None = None,
    model: str = DEFAULT_MODEL,
    width: int = 1024,
    height: int = 1024,
    on_progress=None,
) -> list[Path]:
    """Generate one image per SRT cue. Returns saved paths in cue order."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, cue in enumerate(cues):
        text = (cue.get("text") or "").strip() or f"Scene {i + 1}"
        prompt = _build_prompt(text, style=style, extra=extra)
        data = generate_image(prompt, width=width, height=height, model=model)
        p = out_dir / f"{i + 1:06d}.jpg"
        p.write_bytes(data)
        paths.append(p)
        if on_progress:
            on_progress(i + 1, text)
    return paths


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Generate a free image with Pollinations.ai"
    )
    parser.add_argument("prompt")
    parser.add_argument("--style", default="cinematic",
                        choices=sorted(STYLE_PRESETS))
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        choices=AVAILABLE_MODELS)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--out", default="pollinations.jpg")
    args = parser.parse_args()

    p = _build_prompt(args.prompt, style=args.style)
    data = generate_image(p, width=args.width, height=args.height,
                          model=args.model)
    Path(args.out).write_bytes(data)
    print(args.out)
