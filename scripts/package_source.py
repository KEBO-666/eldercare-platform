"""按源码白名单打包，排除演示数据库、缓存、密钥、虚拟环境及 Git 历史。

python -m scripts.package_source --output dist/competition-source.zip
正式提交时另行加入演示视频，并按报名编号-姓名命名。
"""
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ("app", "scripts", "tests", "docs", "docker")
FILES = ("README.md", "requirements.txt", "pyproject.toml", "pytest.ini", ".env.example", ".gitignore")
EXCLUDED = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "node_modules"}


def package_source(output: Path) -> int:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    paths = [ROOT / name for name in FILES]
    for name in DIRECTORIES:
        paths.extend(path for path in (ROOT / name).rglob("*")
                     if path.is_file() and not EXCLUDED.intersection(path.relative_to(ROOT).parts)
                     and path.suffix not in (".pyc", ".pyo") and path.resolve() != output)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            archive.write(path, "eldercare-platform/" + path.relative_to(ROOT).as_posix())
        for name in ("uploads", "backups"):
            archive.writestr(f"eldercare-platform/data/{name}/.gitkeep", "")
    return len(paths) + 2


def main() -> None:
    parser = argparse.ArgumentParser(description="生成不含运行数据的参赛源码包")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "competition-source.zip")
    args = parser.parse_args()
    count = package_source(args.output)
    print(f"源码包：{args.output.resolve()}，{count} 个文件；尚需演示视频与正式报名命名。")


if __name__ == "__main__":
    main()
