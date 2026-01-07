"""Normalize stored raw submissions to MP3.

Scans SUBMISSIONS_RAW_DIR for audio files, detects non-mp3 content (e.g., WAV renamed to .mp3),
converts to real MP3 via ffmpeg, and:
- writes <uuid>.mp3 next to the original file
- optionally uploads to S3 if configured
- updates TrackSubmission.original_ext to 'mp3' when matching by file_uuid

Run:
    source venv/bin/activate
    python -m trackapp.scripts.normalize_audio --dry-run
    python -m trackapp.scripts.normalize_audio

Options:
    --limit N
    --dry-run
    --keep-original
"""

from __future__ import annotations

import argparse
import os
import subprocess
from datetime import datetime
from pathlib import Path

from trackapp import app, db
from trackapp.models import TrackSubmission
from trackapp.routes import SUBMISSIONS_RAW_DIR, _get_s3_client, _raw_key_for, S3_BUCKET, _content_type_for_ext

def sniff_file_kind(path: str) -> str:
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except Exception:
        return "unknown"
    if head.startswith(b"RIFF") and b"WAVE" in head[8:16]:
        return "wav"
    if head.startswith(b"fLaC"):
        return "flac"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"ID3"):
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    return "unknown"

def convert_to_mp3(in_path: str, out_path: str) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", in_path,
        "-vn",
        "-codec:a", "libmp3lame",
        "-q:a", "2",
        "-map_metadata", "-1",
        out_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:500])

def maybe_upload_s3(file_uuid: str, out_path: str) -> None:
    s3 = _get_s3_client()
    if not s3:
        return
    key = _raw_key_for(file_uuid, "mp3")
    with open(out_path, "rb") as f:
        s3.upload_fileobj(
            Fileobj=f,
            Bucket=S3_BUCKET,
            Key=key,
            ExtraArgs={"ContentType": _content_type_for_ext("mp3")},
        )

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--keep-original", action="store_true")
    args = ap.parse_args()

    raw_dir = Path(SUBMISSIONS_RAW_DIR)
    if not raw_dir.exists():
        print(f"Raw dir not found: {raw_dir}")
        return

    files = sorted([p for p in raw_dir.iterdir() if p.is_file()])
    changed = 0
    seen = 0

    with app.app_context():
        for p in files:
            if args.limit and seen >= args.limit:
                break
            seen += 1

            name = p.name
            if "." not in name:
                continue
            file_uuid, ext = name.split(".", 1)
            ext = ext.lower()
            kind = sniff_file_kind(str(p))
            needs = (kind != "mp3")
            if not needs:
                continue

            out_path = p.with_suffix(".mp3")
            print(f"Convert {p.name} (kind={kind}) -> {out_path.name}")
            if args.dry_run:
                continue

            convert_to_mp3(str(p), str(out_path))
            maybe_upload_s3(file_uuid, str(out_path))

            sub = TrackSubmission.query.filter(TrackSubmission.file_uuid == file_uuid).order_by(TrackSubmission.id.desc()).first()
            if sub:
                sub.original_ext = "mp3"
                sub.updated_at = datetime.utcnow() if hasattr(sub, "updated_at") else None
                db.session.add(sub)

            if not args.keep_original:
                try:
                    p.unlink()
                except Exception:
                    pass

            changed += 1

        if not args.dry_run:
            db.session.commit()

    print(f"Done. converted={changed}")

if __name__ == "__main__":
    main()
