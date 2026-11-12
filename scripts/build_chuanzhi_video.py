"""Assemble a truthful competition video from the supplied demo and evidence slides.

The supplied demonstration remains byte-for-byte available in the original
submission package. This derived video appends a two-minute-plus evidence
section so the reviewer can see the actual AI-assisted iteration records.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
FFMPEG = ROOT / "reports/authoring-venv/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v9.0.2.exe"
FONT = Path("C:/Windows/Fonts/simhei.ttf")
MONO = Path("C:/Windows/Fonts/consola.ttf")
W, H = 1920, 1080
SLIDE_SECONDS = 9.5


def font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def wrap(text: str, fnt: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and fnt.getlength(candidate) > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def add_text(draw: ImageDraw.ImageDraw, text: str, xy: tuple[int, int], fnt, fill, width: int, spacing=14):
    x, y = xy
    for line in wrap(text, fnt, width):
        draw.text((x, y), line, font=fnt, fill=fill)
        y += fnt.size + spacing
    return y


def slide(title: str, kicker: str, paragraphs: list[str], tag: str = "AI协同过程真实记录") -> Image.Image:
    image = Image.new("RGB", (W, H), "#f5f8fa")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, W, 14), fill="#087f8c")
    draw.rectangle((0, H - 62, W, H), fill="#18354c")
    draw.text((90, 74), kicker, font=font(FONT, 25), fill="#087f8c")
    draw.text((90, 120), title, font=font(FONT, 53), fill="#18354c")
    draw.line((90, 214, W - 90, 214), fill="#087f8c", width=3)
    y = 270
    for paragraph in paragraphs:
        draw.ellipse((92, y + 8, 108, y + 24), fill="#f0a43c")
        y = add_text(draw, paragraph, (138, y), font(FONT, 31), "#18354c", W - 250, spacing=14) + 35
    draw.text((90, H - 45), "颐邻 · 社区智慧养老服务与健康监测平台 · 唐克波 · 本科B组 · 2132417", font=font(FONT, 20), fill="#dceef1")
    draw.text((W - 620, H - 45), tag, font=font(FONT, 20), fill="#f6d28a")
    return image


def image_slide(title: str, image_path: Path, note: str) -> Image.Image:
    image = slide(title, "实际浏览器验收素材", [note], tag="自动化浏览器验证")
    source = Image.open(image_path).convert("RGB")
    source.thumbnail((1260, 650))
    x = (W - source.width) // 2
    y = 335
    image.paste(source, (x, y))
    return image


def excerpt_slide(excerpt: dict) -> Image.Image:
    image = slide(excerpt["title"], f"真实可见记录 / 来源 L{excerpt['source_line']}", [])
    draw = ImageDraw.Draw(image)
    y = 265
    text_size = 27 if "\n" in excerpt["text"] else 32
    for line in excerpt["text"].splitlines():
        y = add_text(draw, line, (105, y), font(FONT, text_size), "#18354c", 1700, spacing=12)
    add_text(draw, "来源时间：" + excerpt["timestamp"], (105, 850), font(FONT, 23), "#087f8c", 1700)
    add_text(draw, "展示转换：" + excerpt["presentation_transformation"], (105, 895), font(FONT, 23), "#087f8c", 1700)
    add_text(draw, "原始记录 SHA-256：" + excerpt["source_record_sha256"], (105, 940), font(FONT, 17), "#637789", 1700)
    return image


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def build(source: Path, output: Path, evidence_video: Path | None = None,
          browser_video: Path | None = None) -> dict:
    global FFMPEG
    if not FFMPEG.exists():
        import imageio_ffmpeg
        FFMPEG = Path(imageio_ffmpeg.get_ffmpeg_exe())
    if not FFMPEG.exists():
        raise FileNotFoundError(f"ffmpeg not found: {FFMPEG}")
    work = ROOT / "reports/chuanzhi-video-build"
    work.mkdir(parents=True, exist_ok=True)
    excerpts = json.loads((ROOT / "docs/chuanzhi/evidence/history/video-excerpts.json").read_text(encoding="utf-8"))["excerpts"]
    slides = [
        slide("为什么展示 AI 协同", "VIBE CODING · 证据段落", [
            "比赛要求可复现的开发演进。本段追加到功能演示之后，展示实际可核对的优化记录、失败反馈和验证结果。",
            "原始优化轮是一个用户发起请求中的三个主题索引，不能包装为三条独立完整Prompt链。",
        ]),
        slide("从业务目标到代码约束", "真实优化轮 · 01", [
            "周期预约不是保存一个 weekly 标志，而是实际生成连续四周预约。任一周冲突时，整组创建必须回滚。",
            "派单必须同时满足技能、距离、负荷和时段约束，手工派单也不能绕过。",
        ]),
        excerpt_slide(excerpts[0]),
        excerpt_slide(excerpts[1]),
        excerpt_slide(excerpts[2]),
        slide("权限与退费：负例优先", "真实优化轮 · 03", [
            "未绑定账号不得获得无限制数据；网格和个人账单只能看到可见老人。写操作还要再次校验范围。",
            "部分退费后按原明细剩余余额计算，不能重复整单红冲；CSV/XLSX文本公式也要转义。",
        ]),
        excerpt_slide(excerpts[3]),
        excerpt_slide(excerpts[4]),
        slide("本轮真实输入：并行完成计划", "用户授权 / 新发生迭代", [
            "用户原话：基于此计划，确定环境没有问题之后，不计成本分多个subagents并发为我完成任务。",
            "执行拆分为复现、浏览器质量、AI证据三个分支。主代理负责PDF、视频和集成，按文件范围协作。",
        ]),
        slide("实际反馈：系统Python失效", "主代理定位 → 复现分支修复", [
            "实际运行 python -m venv 失败，旧Python310路径无法启动；现有项目解释器为Python3.12.14。",
            "主代理把真实错误传给复现分支，分支支持显式BasePython、检查退出码，并用有效基础解释器完成冷安装。",
        ]),
        slide("实际反馈：App.toast未导出", "浏览器分支发现 → 基线验证 → 修复", [
            "Edge真实基线显示 typeof App.toast 为 undefined；成功回调调用它会抛错。HTTP状态200不能发现该缺陷。",
            "修复后导出函数、纠正toast可见性与重复提交；浏览器验证真实主线、409恢复及500处置失败保留输入。",
        ]),
        slide("本轮新增的可复现改进", "运行环境与交互反馈", [
            "复现分支修复启动脚本的原生命令错误传播、无效环境检测和配置数据库判断；空副本实测85.563秒完成安装、初始化及13次页面访问。",
            "浏览器分支修复App.toast导出、toast可见性、重复点击保护、失败重试、候选实际权重与适老化按钮。",
        ]),
        slide("证据边界与提交材料", "真实披露", [
            "证据副本排除私有推理和系统内部指令，保留可见记录、来源行号和哈希；当前没有可公开的远程仓库。",
            "本视频的功能段来自参赛人提供的原始演示；末尾证据段为本轮真实记录的可读展示，不声称已满足三条独立Prompt链。",
        ], tag="材料索引见技术文档"),
    ]
    slide_dir = work / "slides"
    slide_dir.mkdir(exist_ok=True)
    for index, image in enumerate(slides, 1):
        image.save(slide_dir / f"slide-{index:02}.png")
    evidence = work / "evidence.mp4"
    if evidence_video:
        run([str(FFMPEG), "-y", "-i", str(evidence_video), "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
             "-vf", "scale=1920:1080,fps=30", "-t", "122", "-c:v", "libx264", "-preset", "medium",
             "-crf", "20", "-c:a", "aac", "-b:a", "96k", "-pix_fmt", "yuv420p", str(evidence)])
    else:
        run([str(FFMPEG), "-y", "-framerate", "2/19", "-i", str(slide_dir / "slide-%02d.png"),
             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-vf", "fps=30",
             "-t", str(len(slides) * SLIDE_SECONDS), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-c:a", "aac", "-b:a", "96k", "-pix_fmt", "yuv420p", str(evidence)])
    normalized = work / "supplied-normalized.mp4"
    source_args = ["-t", "230"] if browser_video else []
    run([str(FFMPEG), "-y", "-i", str(source), *source_args, "-r", "30", "-c:v", "libx264", "-preset", "medium",
         "-crf", "20", "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", str(normalized)])
    concat = work / "concat.txt"
    segments = [normalized]
    if browser_video:
        browser_clip = work / "browser-current.mp4"
        run([str(FFMPEG), "-y", "-i", str(browser_video), "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
             "-vf", "scale=1920:1080,fps=30", "-t", "77", "-c:v", "libx264", "-preset", "medium",
             "-crf", "20", "-c:a", "aac", "-b:a", "96k", "-pix_fmt", "yuv420p", str(browser_clip)])
        segments.append(browser_clip)
    segments.append(evidence)
    concat.write_text("".join(f"file '{segment.as_posix()}'\n" for segment in segments), encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    run([str(FFMPEG), "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy",
         "-movflags", "+faststart", "-metadata", "title=颐邻：社区智慧养老服务与健康监测平台",
         "-metadata", "comment=功能演示加真实AI协同证据段；见技术文档的证据边界", str(output)])
    probe = subprocess.run([str(FFMPEG), "-hide_banner", "-i", str(output)], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
    text = probe.stderr
    duration = None
    import re
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", text)
    if match:
        duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    data = {
        "output": str(output), "source": str(source), "duration_seconds": duration,
        "source_duration_seconds": 348.86, "evidence_slides": len(slides),
        "source_segment_seconds": 230 if browser_video else 348.86,
        "current_browser_source": str(browser_video) if browser_video else None,
        "current_browser_seconds": 77 if browser_video else 0,
        "evidence_seconds": 122 if evidence_video else len(slides) * SLIDE_SECONDS, "resolution": "1920x1080",
        "format": "MP4/H.264/AAC", "ai_evidence_is_appended_slides": evidence_video is None,
        "interactive_evidence_source": str(evidence_video) if evidence_video else None,
        "integrity": "feature demo normalized from supplied video; evidence is a labeled replay of actual visible records and live checks",
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "bytes": output.stat().st_size,
    }
    output.with_suffix(".json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=ROOT / "output/video/2132417-唐克波-演示视频.mp4")
    parser.add_argument("--evidence-video", type=Path)
    parser.add_argument("--browser-video", type=Path)
    args = parser.parse_args()
    source = args.source or next(Path("C:/Users/Administrator").glob("PYTH260036ON-*.mp4"))
    print(json.dumps(build(source, args.output, args.evidence_video, args.browser_video), ensure_ascii=False, indent=2))
