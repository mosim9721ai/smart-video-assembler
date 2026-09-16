"""Free-tier backend: Pollinations.ai (images + LLM) + edge-tts (TTS).

No API keys, no billing. Drop-in replacements for the fal.ai helpers in app.py.
Signatures match so cli.py can swap the functions in without changes.
"""

import asyncio
import json
import os
import re
import tempfile
import urllib.parse

import requests

# Pollinations dims — larger than Fal presets since it's free
RATIO_DIMS = {
    "9:16": (720, 1280),
    "16:9": (1280, 720),
    "1:1": (1024, 1024),
}

# Edge-TTS voices per detected language.
# BrianMultilingual is a deep dramatic voice — great for narration.
FREE_TTS_VOICES = {
    "hi": "hi-IN-MadhurNeural",
    "en": "en-US-BrianMultilingualNeural",
}


def _detect_lang(text):
    devanagari = sum(1 for c in text if "ऀ" <= c <= "ॿ")
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    if devanagari and devanagari > latin * 0.3:
        return "hi"
    return "en"


def resolve_tts(text, model_override, voice_override):
    """Free-tier version of resolve_tts. Returns (model, voice, lang)."""
    lang = _detect_lang(text)
    default_voice = FREE_TTS_VOICES[lang]
    voice = (voice_override or "").strip()
    if not voice or voice.lower() == "auto":
        voice = default_voice
    # Model name here is just a label — edge-tts doesn't have model selection.
    return "edge-tts", voice, lang


def generate_one_image(model, prompt, ratio):
    """Image via Pollinations. `model` is ignored (or passed as pollinations model)."""
    w, h = RATIO_DIMS.get(ratio, RATIO_DIMS["9:16"])
    # Pollinations prompt cap ~2000 chars; trim if needed.
    encoded = urllib.parse.quote(prompt[:1800])
    seed = abs(hash(prompt)) % 1_000_000
    poll_model = "flux"
    if model and "sdxl" in model.lower():
        poll_model = "turbo"
    url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width={w}&height={h}&seed={seed}&nologo=true&model={poll_model}"
    )
    r = requests.get(url, timeout=180)
    r.raise_for_status()
    return r.content


def tts_one_chunk(text, model, voice):
    """TTS via edge-tts. `model` is ignored — edge-tts is one system."""
    import edge_tts  # imported lazily so the module still loads if not installed

    async def _gen():
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(tmp_path)
            with open(tmp_path, "rb") as f:
                return f.read()
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    return asyncio.run(_gen())


def generate_visual_prompts(chunks, style, llm_model, submodel):
    """Visual prompts via Pollinations text API (uses openai model behind the scenes).
       Falls back to `chunk + style` if the LLM call fails."""
    style = (style or "cinematic, natural lighting, film grain, 4k").strip()
    scenes = "\n".join(f"[{i+1}] {c}" for i, c in enumerate(chunks))

    system = (
        "You are a visual prompt engineer for AI image generation. "
        "For each numbered scene, write ONE concrete, camera-ready English image prompt "
        "(subject + setting + lighting + framing). Keep characters/locations consistent. "
        f"Return ONLY a JSON array of exactly {len(chunks)} strings. No prose."
    )
    user = (
        f"Style guide: {style}\n\n"
        f"Scenes (in order):\n{scenes}\n\n"
        f"Output: JSON array of {len(chunks)} strings."
    )

    try:
        r = requests.post(
            "https://text.pollinations.ai/openai",
            json={
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "model": "openai",
                "jsonMode": True,
            },
            timeout=90,
        )
        r.raise_for_status()
        data = r.json()
        # Pollinations OpenAI-compat: choices[0].message.content
        content = ""
        if isinstance(data, dict):
            if "choices" in data:
                content = data["choices"][0]["message"]["content"]
            else:
                content = data.get("output") or data.get("content") or str(data)
        else:
            content = str(data)

        m = re.search(r"\[.*\]", content, re.DOTALL)
        if m:
            prompts = json.loads(m.group(0))
            prompts = [str(p).strip() for p in prompts if str(p).strip()]
            if len(prompts) >= len(chunks):
                return prompts[: len(chunks)]
            if prompts:
                return prompts + [prompts[-1]] * (len(chunks) - len(prompts))
    except Exception as e:
        print(f"⚠️  Pollinations LLM failed ({e}); using script chunks + style as prompts.")

    # Fallback: script chunk itself as the visual prompt
    return [f"{c}, {style}" for c in chunks]
