"""
Generate images from text prompts using Google's Gemini image models.

Default model is `gemini-2.5-flash-image-preview` (also known as
"Nano Banana"). The API key is supplied per-request — never stored — and
comes from the caller.

Public entry points:
    generate_image(prompt, api_key, ...) -> bytes (PNG)
    generate_for_cues(cues, api_key, ...) -> list[Path]
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable

DEFAULT_MODEL = "gemini-2.5-flash-image"
API_HOST = "https://generativelanguage.googleapis.com"

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


class GeminiError(RuntimeError):
    """Raised for any Gemini API failure. Never contains the API key."""


def _build_prompt(cue_text: str, style: str = "cinematic",
                  extra: str | None = None) -> str:
    style_hint = STYLE_PRESETS.get(style, STYLE_PRESETS["cinematic"])
    parts = [f"Scene: {cue_text.strip()}", style_hint]
    if extra:
        parts.append(extra.strip())
    parts.append("No text, no captions, no watermarks in the image.")
    return ". ".join(parts)


def generate_image(
    prompt: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout: float = 90.0,
    retries: int = 2,
) -> bytes:
    """Call the Gemini image model and return raw PNG bytes.

    Retries transient failures (429, 5xx, network errors) with backoff.
    """
    if not api_key:
        raise GeminiError("Missing API key")
    if not prompt.strip():
        raise GeminiError("Empty prompt")

    url = f"{API_HOST}/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    data = json.dumps(body).encode("utf-8")

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return _extract_image(payload)
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")[:1000]
            # Never echo the API key back — the URL and headers are already
            # scrubbed since we do not surface them.
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                last_err = GeminiError(f"HTTP {e.code}: {body_text}")
                time.sleep(2 ** attempt)
                continue
            raise GeminiError(f"HTTP {e.code}: {body_text}") from None
        except urllib.error.URLError as e:
            if attempt < retries:
                last_err = GeminiError(f"Network error: {e.reason}")
                time.sleep(2 ** attempt)
                continue
            raise GeminiError(f"Network error: {e.reason}") from None

    raise last_err or GeminiError("Unknown failure")


def _extract_image(payload: dict) -> bytes:
    """Pull the first inline image out of a generateContent response."""
    try:
        candidates = payload.get("candidates") or []
        for cand in candidates:
            parts = ((cand.get("content") or {}).get("parts")) or []
            for part in parts:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])
    except Exception:
        pass
    # If we get here, the model returned only text or an error message.
    text_bits: list[str] = []
    for cand in payload.get("candidates") or []:
        for part in ((cand.get("content") or {}).get("parts")) or []:
            if isinstance(part.get("text"), str):
                text_bits.append(part["text"])
    if payload.get("promptFeedback"):
        text_bits.append(json.dumps(payload["promptFeedback"]))
    detail = " | ".join(text_bits)[:500] or "No image data in response"
    raise GeminiError(f"No image returned: {detail}")


def generate_for_cues(
    cues: Iterable[dict],
    api_key: str,
    out_dir: str | Path,
    style: str = "cinematic",
    extra: str | None = None,
    model: str = DEFAULT_MODEL,
    ext: str = "png",
    on_progress=None,
) -> list[Path]:
    """Generate one image per SRT cue and save them numbered in out_dir.

    Each cue is a dict with at least a ``text`` field (as produced by the
    existing parse_srt in app.py). Returns the list of written file paths in
    cue order.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, cue in enumerate(cues):
        text = (cue.get("text") or "").strip()
        if not text:
            text = f"Scene {i + 1}"
        prompt = _build_prompt(text, style=style, extra=extra)
        img_bytes = generate_image(prompt, api_key, model=model)
        p = out_dir / f"{i + 1:06d}.{ext}"
        p.write_bytes(img_bytes)
        paths.append(p)
        if on_progress:
            on_progress(i + 1, text)
    return paths


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate an image with Gemini (Nano Banana)"
    )
    parser.add_argument("prompt", help="What to draw")
    parser.add_argument("--style", default="cinematic",
                        choices=sorted(STYLE_PRESETS.keys()))
    parser.add_argument("--out", default="out.png")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-key", default=os.environ.get("GEMINI_API_KEY"))
    args = parser.parse_args()

    if not args.api_key:
        raise SystemExit(
            "Set GEMINI_API_KEY or pass --api-key. "
            "Get one at https://aistudio.google.com/apikey"
        )

    prompt = _build_prompt(args.prompt, style=args.style)
    data = generate_image(prompt, args.api_key, model=args.model)
    Path(args.out).write_bytes(data)
    print(args.out)
