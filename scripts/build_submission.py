"""将源码、设计文档和已完成视频合并为正式命名的提交包。"""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path

from scripts.package_source import ROOT, package_source


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_submission(video: Path, entry: str, output_dir: Path) -> Path:
    video = video.resolve(strict=True)
    if not video.is_file() or video.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm", ".avi"}:
        raise ValueError("请提供有效的视频文件路径")
    if not entry or any(char in entry for char in '<>:"/\\|?*'):
        raise ValueError("报名编号-姓名不能包含文件名非法字符")
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{entry}.zip"
    video_name = f"演示视频/{video.name}"
    instructions = f"""# 比赛作品提交说明

报名信息：{entry}
作品：社区智慧养老服务与健康监测平台

## 提交内容

- 完整项目源码：eldercare-platform/（含依赖清单、启动脚本、测试和本地静态资源）
- 系统设计文档：eldercare-platform/docs/design.md
- 第三方来源与许可证：eldercare-platform/docs/第三方依赖与许可证.md
- 验证报告：eldercare-platform/docs/test-report.md
- 已完成演示视频：{video_name}
- 文件校验清单：manifest.json（逐文件大小与 SHA-256）

## 运行方法

解压后在 eldercare-platform 目录打开 PowerShell：

```powershell
python -m venv .venv
.\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File scripts/run_dev.ps1
```

访问 http://127.0.0.1:8000 ，管理员账号 super，密码 Admin@123。
其他演示账号：street、orgadmin、grid1、nurse1、elder1，密码相同。
首次启动由种子脚本生成虚构演示数据，不含已有数据库或个人密钥。

## 验证与说明

已有验证：410 项自动化测试通过、语句覆盖率 89.14%，六类账号真实 HTTP 登录与 13 次页面访问通过。
本次打包校验 ZIP 完整性、逐文件摘要与视频字节一致性；不代表已人工审阅视频的全部内容。
功能边界及未完成验收见 eldercare-platform/docs/赛题验收优化说明.md。
不包含虚拟环境、依赖缓存、生产数据库、上传附件、实际 .env 或 Git 历史。
"""
    records = []
    with tempfile.TemporaryDirectory(prefix="eldercare-package-") as directory:
        source_zip = Path(directory) / "source.zip"
        package_source(source_zip)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
            with zipfile.ZipFile(source_zip) as source:
                for member in source.infolist():
                    content = source.read(member)
                    target.writestr(member.filename, content)
                    records.append({"path": member.filename, "bytes": len(content),
                                    "sha256": hashlib.sha256(content).hexdigest()})
            target.writestr("提交说明.md", instructions.encode("utf-8"))
            target.write(video, video_name, compress_type=zipfile.ZIP_STORED)
            records.extend([
                {"path": "提交说明.md", "bytes": len(instructions.encode("utf-8")),
                 "sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest()},
                {"path": video_name, "bytes": video.stat().st_size, "sha256": file_hash(video)},
            ])
            target.writestr("manifest.json", json.dumps(
                {"entry": entry, "files": records}, ensure_ascii=False, indent=2,
            ).encode("utf-8"))
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("压缩包完整性校验失败")
        for record in records:
            digest = hashlib.sha256()
            with archive.open(record["path"]) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != record["sha256"]:
                raise RuntimeError(f"文件摘要不一致：{record['path']}")
    output.with_suffix(".zip.sha256").write_text(
        f"{file_hash(output)}  {output.name}\n", encoding="utf-8",
    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="生成包含演示视频的比赛提交压缩包")
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--entry", required=True, help="报名编号-姓名")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    output = build_submission(args.video, args.entry, args.output_dir)
    print(f"提交包已生成并通过文件摘要校验：{output.resolve()}（{output.stat().st_size} 字节）")


if __name__ == "__main__":
    main()
