from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from typing import Optional, Tuple

# Magic bytes helpers

def sniff_audio_kind(data: bytes, filename: str | None = None) -> str:
    """Return best-effort kind: mp3|wav|flac|ogg|unknown."""
    if not data:
        return "unknown"
    head = data[:64]
    # WAV: RIFF....WAVE
    if head.startswith(b"RIFF") and b"WAVE" in head[8:16]:
        return "wav"
    # FLAC: fLaC
    if head.startswith(b"fLaC"):
        return "flac"
    # OGG: OggS
    if head.startswith(b"OggS"):
        return "ogg"
    # MP3: ID3 tag or frame sync 0xFF Ex
    if head.startswith(b"ID3"):
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    return "unknown"


async def _which(cmd: str) -> Optional[str]:
    # asyncio-friendly which
    # NOTE: systemd units often run with a very minimal PATH (e.g. only venv/bin).
    # Add common system locations so ffmpeg can be found even when PATH is restricted.
    raw_paths = [p for p in os.getenv("PATH", "").split(os.pathsep) if p]
    raw_paths += [
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]

    seen = set()
    paths: list[str] = []
    for p in raw_paths:
        if p not in seen:
            seen.add(p)
            paths.append(p)

    for p in paths:
        cand = os.path.join(p, cmd)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


async def convert_bytes_to_mp3(file_bytes: bytes, *, input_ext: str, ffmpeg_path: str = "ffmpeg") -> bytes:
    """Convert arbitrary audio bytes to mp3 using ffmpeg. Returns mp3 bytes.

    Uses temp files to avoid piping issues with some formats.
    """
    if not file_bytes:
        raise ValueError("empty file")

    # Ensure ffmpeg exists
    if ffmpeg_path == "ffmpeg":
        found = await _which("ffmpeg")
        if not found:
            raise RuntimeError("ffmpeg is not installed on server")
        ffmpeg_path = found

    input_ext = (input_ext or "").lower().lstrip(".") or "bin"

    with tempfile.TemporaryDirectory(prefix="trackrater_audio_") as td:
        in_path = os.path.join(td, f"in.{input_ext}")
        out_path = os.path.join(td, "out.mp3")
        with open(in_path, "wb") as f:
            f.write(file_bytes)

        # -vn: no video, -q:a 2: good VBR quality, -map_metadata -1 to avoid weird tags
        proc = await asyncio.create_subprocess_exec(
            ffmpeg_path,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", in_path,
            "-vn",
            "-codec:a", "libmp3lame",
            "-q:a", "2",
            "-map_metadata", "-1",
            out_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", "ignore").strip()
            raise RuntimeError(f"ffmpeg convert failed (rc={proc.returncode}): {err[:500]}")

        with open(out_path, "rb") as f:
            return f.read()
