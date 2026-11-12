"""Validate final PDF/video formats, decode video, and create visual QA frames."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    video = ROOT / "output/video/2132417-唐克波-演示视频.mp4"
    pdf = ROOT / "output/pdf/2132417-唐克波-技术文档.pdf"
    metadata = json.loads(video.with_suffix(".json").read_text(encoding="utf-8"))
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    probe = subprocess.run([ffmpeg, "-hide_banner", "-i", str(video)], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
    duration = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", probe.stderr)
    if duration is None:
        raise RuntimeError("Cannot determine final video duration")
    seconds = int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])
    if not 300 <= seconds <= 480 or "1920x1080" not in probe.stderr:
        raise RuntimeError("Video duration or resolution does not meet the provided rules")
    decode = subprocess.run([ffmpeg, "-v", "error", "-i", str(video), "-f", "null", "-"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if decode.returncode or decode.stderr.strip():
        raise RuntimeError("Video decode failed: " + decode.stderr)
    if metadata["evidence_seconds"] < 120:
        raise RuntimeError("AI evidence replay is shorter than two minutes")
    sha = hashlib.sha256(video.read_bytes()).hexdigest()
    if sha != metadata["sha256"]:
        raise RuntimeError("Video differs from its builder metadata")
    reader = PdfReader(pdf)
    if not 1 <= len(reader.pages) <= 30:
        raise RuntimeError("PDF page count outside the required limit")
    pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if not all(word in pdf_text for word in ("2132417", "唐克波", "意图", "复现")):
        raise RuntimeError("PDF text validation failed")
    qa = ROOT / "reports/chuanzhi-video-qa"
    qa.mkdir(parents=True, exist_ok=True)
    times = [0, 150, 232, 280, 311, 365, 395, max(0, seconds - 8)]
    thumbnails = []
    for index, when in enumerate(times):
        path = qa / f"frame-{index:02}.png"
        subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", str(when), "-i", str(video),
                        "-frames:v", "1", str(path)], check=True)
        picture = Image.open(path).convert("RGB")
        picture.thumbnail((480, 270))
        thumbnails.append(picture)
    sheet = Image.new("RGB", (4 * 500, 2 * 305), "#dce5ec")
    draw = ImageDraw.Draw(sheet)
    for index, picture in enumerate(thumbnails):
        x, y = index % 4 * 500 + 10, index // 4 * 305 + 8
        sheet.paste(picture, (x, y))
        draw.text((x, y + 275), f"{times[index]:.2f}s", fill="#18354c")
    sheet.save(qa / "contact.png")
    evidence_path = Path(metadata["interactive_evidence_source"])
    evidence_run = evidence_path.parent.parent
    evidence = json.loads((evidence_run / "result.json").read_text(encoding="utf-8"))
    if not evidence["all_commands_passed"]:
        raise RuntimeError("The included interactive evidence recording contains failed live checks")
    result = {
        "passed": True, "duration_seconds": seconds, "resolution": "1920x1080",
        "decode_all_frames": "passed", "evidence_replay_seconds": metadata["evidence_seconds"],
        "live_evidence_checks": evidence["all_commands_passed"], "video_sha256": sha,
        "pdf_pages": len(reader.pages), "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "pdf_text_check": "passed", "visual_review": "pending", "contact_sheet": str(qa / "contact.png"),
        "notice": "Format and integrity checks do not certify eligibility or three full independent Prompt chains.",
    }
    (ROOT / "reports/chuanzhi-artifacts-qa.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
