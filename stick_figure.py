"""
Stick figure animation generator.

Draws parameterised stick figures with PIL and encodes the frame sequence to MP4
with FFmpeg. No external API, no account, no per-frame cost.

Public entry points:
    render_animation(action, duration, ...) -> path to MP4
    ANIMATIONS -> dict of supported actions

Each animation function takes a normalised time `t` in [0, 1) and returns a
Pose (joint positions in a unit coordinate system where the ground is y=1 and
the horizontal center is x=0.5).
"""

from __future__ import annotations

import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont


@dataclass
class Pose:
    """Joint positions in a normalised unit square (0..1 on each axis)."""
    head: tuple[float, float]
    neck: tuple[float, float]
    hip: tuple[float, float]
    left_hand: tuple[float, float]
    right_hand: tuple[float, float]
    left_foot: tuple[float, float]
    right_foot: tuple[float, float]
    left_elbow: tuple[float, float] | None = None
    right_elbow: tuple[float, float] | None = None
    left_knee: tuple[float, float] | None = None
    right_knee: tuple[float, float] | None = None


def _mid(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


# --- Animations ------------------------------------------------------------


def pose_idle(t: float, cx: float = 0.5) -> Pose:
    bob = 0.008 * math.sin(t * 2 * math.pi * 2)
    hip = (cx, 0.62 + bob)
    neck = (cx, 0.42 + bob)
    head = (cx, 0.32 + bob)
    return Pose(
        head=head,
        neck=neck,
        hip=hip,
        left_hand=(cx - 0.10, 0.60 + bob),
        right_hand=(cx + 0.10, 0.60 + bob),
        left_foot=(cx - 0.06, 0.92),
        right_foot=(cx + 0.06, 0.92),
    )


def pose_waving(t: float, cx: float = 0.5) -> Pose:
    base = pose_idle(t, cx)
    swing = math.sin(t * 2 * math.pi * 3)
    hand_x = cx + 0.20 + 0.06 * swing
    hand_y = 0.22 + 0.03 * math.sin(t * 2 * math.pi * 6)
    elbow = (cx + 0.13, 0.38)
    return Pose(
        head=base.head,
        neck=base.neck,
        hip=base.hip,
        left_hand=base.left_hand,
        right_hand=(hand_x, hand_y),
        left_foot=base.left_foot,
        right_foot=base.right_foot,
        right_elbow=elbow,
    )


def pose_walking(t: float, cx: float = 0.5) -> Pose:
    # Move across the frame left-to-right.
    x = _lerp(0.15, 0.85, t)
    phase = t * 2 * math.pi * 4  # 2 full stride cycles per second-ish
    swing = math.sin(phase)
    bob = 0.010 * abs(math.sin(phase * 2))
    hip = (x, 0.62 + bob)
    neck = (x, 0.42 + bob)
    head = (x, 0.32 + bob)
    return Pose(
        head=head,
        neck=neck,
        hip=hip,
        left_hand=(x - 0.09 - 0.05 * swing, 0.58 + bob),
        right_hand=(x + 0.09 + 0.05 * swing, 0.58 + bob),
        left_foot=(x - 0.02 + 0.10 * swing, 0.92 - max(0, 0.05 * swing)),
        right_foot=(x + 0.02 - 0.10 * swing, 0.92 - max(0, -0.05 * swing)),
        left_knee=(x - 0.02 + 0.06 * swing, 0.78),
        right_knee=(x + 0.02 - 0.06 * swing, 0.78),
    )


def pose_running(t: float, cx: float = 0.5) -> Pose:
    x = _lerp(0.10, 0.90, t)
    phase = t * 2 * math.pi * 6
    swing = math.sin(phase)
    lean = 0.03  # forward lean
    bob = 0.02 * abs(math.sin(phase * 2))
    hip = (x, 0.62 + bob)
    neck = (x + lean, 0.42 + bob)
    head = (x + lean * 1.5, 0.30 + bob)
    return Pose(
        head=head,
        neck=neck,
        hip=hip,
        left_hand=(x - 0.14 * swing, 0.50 + bob),
        right_hand=(x + 0.14 * swing, 0.50 + bob),
        left_foot=(x + 0.14 * swing, 0.92 - max(0, 0.08 * swing)),
        right_foot=(x - 0.14 * swing, 0.92 - max(0, -0.08 * swing)),
    )


def pose_jumping(t: float, cx: float = 0.5) -> Pose:
    cycle = (t * 2) % 1.0  # 2 jumps per full duration
    # A quick crouch then leap: parabolic lift after 20% of each cycle.
    if cycle < 0.2:
        lift = -0.03 * math.sin(cycle / 0.2 * math.pi)  # crouch down
    else:
        u = (cycle - 0.2) / 0.8
        lift = 0.30 * math.sin(u * math.pi)  # up and back down
    hip_y = 0.62 - lift
    neck_y = 0.42 - lift
    head_y = 0.32 - lift
    foot_lift = max(0.0, lift * 0.6)
    arm_lift = 0.15 * math.sin(min(1.0, cycle / 0.5) * math.pi)
    return Pose(
        head=(cx, head_y),
        neck=(cx, neck_y),
        hip=(cx, hip_y),
        left_hand=(cx - 0.18, 0.55 - arm_lift * 2),
        right_hand=(cx + 0.18, 0.55 - arm_lift * 2),
        left_foot=(cx - 0.06, 0.92 - foot_lift),
        right_foot=(cx + 0.06, 0.92 - foot_lift),
    )


def pose_dancing(t: float, cx: float = 0.5) -> Pose:
    beat = t * 2 * math.pi * 4
    sway = 0.05 * math.sin(beat)
    bob = 0.015 * math.sin(beat * 2)
    hip = (cx + sway, 0.62 + bob)
    neck = (cx + sway * 0.6, 0.42 + bob)
    head = (cx + sway * 0.4, 0.32 + bob)
    arm_up = 0.20 * (0.5 + 0.5 * math.sin(beat))
    return Pose(
        head=head,
        neck=neck,
        hip=hip,
        left_hand=(cx - 0.18 + sway, 0.25 - arm_up),
        right_hand=(cx + 0.18 + sway, 0.25 - arm_up * (1 - 0.4 * math.sin(beat))),
        left_foot=(cx - 0.08 + sway, 0.92 - max(0, 0.03 * math.sin(beat))),
        right_foot=(cx + 0.08 + sway, 0.92 - max(0, -0.03 * math.sin(beat))),
    )


def pose_thinking(t: float, cx: float = 0.5) -> Pose:
    base = pose_idle(t, cx)
    # One hand up to the chin.
    hand = (cx + 0.06, 0.34)
    return Pose(
        head=base.head,
        neck=base.neck,
        hip=base.hip,
        left_hand=base.left_hand,
        right_hand=hand,
        left_foot=base.left_foot,
        right_foot=base.right_foot,
        right_elbow=(cx + 0.10, 0.48),
    )


ANIMATIONS: dict[str, Callable[[float, float], Pose]] = {
    "idle": pose_idle,
    "waving": pose_waving,
    "walking": pose_walking,
    "running": pose_running,
    "jumping": pose_jumping,
    "dancing": pose_dancing,
    "thinking": pose_thinking,
}


# --- Rendering -------------------------------------------------------------

BG_LIGHT = (245, 246, 248)
BG_DARK = (18, 20, 24)
INK_LIGHT = (28, 30, 34)
INK_DARK = (232, 234, 238)
ACCENT = (255, 138, 76)


def _to_px(p: tuple[float, float], w: int, h: int) -> tuple[float, float]:
    return (p[0] * w, p[1] * h)


def _draw_pose(
    img: Image.Image,
    pose: Pose,
    theme: str = "light",
    caption: str | None = None,
) -> None:
    w, h = img.size
    d = ImageDraw.Draw(img)
    ink = INK_DARK if theme == "dark" else INK_LIGHT

    # Ground line for readability.
    ground_y = 0.93 * h
    d.line([(0.05 * w, ground_y), (0.95 * w, ground_y)],
           fill=ink, width=max(2, w // 400))

    head = _to_px(pose.head, w, h)
    neck = _to_px(pose.neck, w, h)
    hip = _to_px(pose.hip, w, h)
    lh = _to_px(pose.left_hand, w, h)
    rh = _to_px(pose.right_hand, w, h)
    lf = _to_px(pose.left_foot, w, h)
    rf = _to_px(pose.right_foot, w, h)

    stroke = max(4, w // 200)
    head_r = max(14, w // 40)

    # Head (outline + fill so it reads on both themes).
    d.ellipse(
        (head[0] - head_r, head[1] - head_r,
         head[0] + head_r, head[1] + head_r),
        outline=ink, width=stroke,
    )
    # Simple smiling face.
    eye_r = max(2, stroke // 2)
    d.ellipse((head[0] - head_r * 0.4 - eye_r, head[1] - head_r * 0.2 - eye_r,
               head[0] - head_r * 0.4 + eye_r, head[1] - head_r * 0.2 + eye_r),
              fill=ink)
    d.ellipse((head[0] + head_r * 0.4 - eye_r, head[1] - head_r * 0.2 - eye_r,
               head[0] + head_r * 0.4 + eye_r, head[1] - head_r * 0.2 + eye_r),
              fill=ink)
    d.arc(
        (head[0] - head_r * 0.5, head[1] - head_r * 0.1,
         head[0] + head_r * 0.5, head[1] + head_r * 0.6),
        start=20, end=160, fill=ink, width=max(2, stroke // 2),
    )

    # Body.
    d.line([neck, hip], fill=ink, width=stroke)

    # Arms — bend through elbow if provided.
    if pose.left_elbow is not None:
        le = _to_px(pose.left_elbow, w, h)
        d.line([neck, le], fill=ink, width=stroke)
        d.line([le, lh], fill=ink, width=stroke)
    else:
        d.line([neck, lh], fill=ink, width=stroke)

    if pose.right_elbow is not None:
        re = _to_px(pose.right_elbow, w, h)
        d.line([neck, re], fill=ink, width=stroke)
        d.line([re, rh], fill=ink, width=stroke)
    else:
        d.line([neck, rh], fill=ink, width=stroke)

    # Legs — bend through knee if provided.
    if pose.left_knee is not None:
        lk = _to_px(pose.left_knee, w, h)
        d.line([hip, lk], fill=ink, width=stroke)
        d.line([lk, lf], fill=ink, width=stroke)
    else:
        d.line([hip, lf], fill=ink, width=stroke)

    if pose.right_knee is not None:
        rk = _to_px(pose.right_knee, w, h)
        d.line([hip, rk], fill=ink, width=stroke)
        d.line([rk, rf], fill=ink, width=stroke)
    else:
        d.line([hip, rf], fill=ink, width=stroke)

    if caption:
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                max(18, w // 30),
            )
        except OSError:
            font = ImageFont.load_default()
        bbox = d.textbbox((0, 0), caption, font=font)
        tw = bbox[2] - bbox[0]
        d.text(((w - tw) / 2, 0.05 * h), caption, fill=ACCENT, font=font)


def render_animation(
    action: str,
    duration: float = 3.0,
    fps: int = 30,
    size: tuple[int, int] = (720, 720),
    theme: str = "light",
    caption: str | None = None,
    output: str | Path | None = None,
    workdir: str | Path | None = None,
) -> Path:
    """Render one animation to an MP4 and return its path."""
    if action not in ANIMATIONS:
        raise ValueError(
            f"Unknown action {action!r}. Available: {sorted(ANIMATIONS)}"
        )
    if duration <= 0:
        raise ValueError("duration must be > 0")
    if fps < 1:
        raise ValueError("fps must be >= 1")

    pose_fn = ANIMATIONS[action]
    frames = max(1, int(round(duration * fps)))
    w, h = size

    workdir = Path(workdir) if workdir else Path("stick_frames")
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    bg = BG_DARK if theme == "dark" else BG_LIGHT
    for i in range(frames):
        t = i / frames
        img = Image.new("RGB", (w, h), bg)
        pose = pose_fn(t, 0.5)
        _draw_pose(img, pose, theme=theme, caption=caption)
        img.save(workdir / f"frame_{i:06d}.png")

    output = Path(output) if output else Path(f"{action}.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(fps),
        "-i", str(workdir / "frame_%06d.png"),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "ultrafast",
        "-crf", "22",
        "-movflags", "+faststart",
        str(output),
    ]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stdout[-4000:])
    return output


def render_sequence(
    scenes: list[dict],
    fps: int = 30,
    size: tuple[int, int] = (720, 720),
    theme: str = "light",
    output: str | Path | None = None,
    workdir: str | Path | None = None,
) -> Path:
    """Render a multi-scene story to a single MP4.

    Each scene is a dict: {"action": str, "duration": float, "caption": str?}.
    """
    if not scenes:
        raise ValueError("scenes must not be empty")

    workdir = Path(workdir) if workdir else Path("stick_seq")
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    frame_idx = 0
    w, h = size
    bg = BG_DARK if theme == "dark" else BG_LIGHT
    for scene in scenes:
        action = scene["action"]
        duration = float(scene.get("duration", 2.0))
        caption = scene.get("caption")
        if action not in ANIMATIONS:
            raise ValueError(f"Unknown action {action!r} in scene {scene}")
        pose_fn = ANIMATIONS[action]
        frames = max(1, int(round(duration * fps)))
        for i in range(frames):
            t = i / frames
            img = Image.new("RGB", (w, h), bg)
            pose = pose_fn(t, 0.5)
            _draw_pose(img, pose, theme=theme, caption=caption)
            img.save(workdir / f"frame_{frame_idx:06d}.png")
            frame_idx += 1

    output = Path(output) if output else Path("stick_sequence.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(fps),
        "-i", str(workdir / "frame_%06d.png"),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "ultrafast",
        "-crf", "22",
        "-movflags", "+faststart",
        str(output),
    ]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stdout[-4000:])
    return output


if __name__ == "__main__":
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(description="Stick figure video generator")
    parser.add_argument("action", nargs="?", default="walking",
                        choices=sorted(ANIMATIONS.keys()) + ["sequence"])
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--width", type=int, default=720)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--theme", choices=["light", "dark"], default="light")
    parser.add_argument("--caption", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--scenes", default=None,
                        help='JSON list of {"action","duration","caption"?} '
                             'for --action=sequence')
    args = parser.parse_args()

    if args.action == "sequence":
        if not args.scenes:
            raise SystemExit("--scenes JSON required with action=sequence")
        scenes = _json.loads(args.scenes)
        path = render_sequence(
            scenes,
            fps=args.fps,
            size=(args.width, args.height),
            theme=args.theme,
            output=args.output,
        )
    else:
        path = render_animation(
            args.action,
            duration=args.duration,
            fps=args.fps,
            size=(args.width, args.height),
            theme=args.theme,
            caption=args.caption,
            output=args.output,
        )
    print(str(path))
