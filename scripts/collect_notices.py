"""从当前安装元数据和本地资源声明收集第三方来源及许可证。"""
from __future__ import annotations

import importlib.metadata as metadata
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    destination = ROOT / "docs" / "licenses"
    destination.mkdir(parents=True, exist_ok=True)
    rows = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        requirement = line.strip()
        if not requirement or requirement.startswith("#"):
            continue
        name, version = requirement.split("==", 1)
        distribution = metadata.distribution(name)
        if distribution.version != version:
            raise RuntimeError(f"{name} 安装版本与依赖清单不一致")
        license_name = distribution.metadata.get("License-Expression") or distribution.metadata.get("License")
        if not license_name:
            license_name = {"fastapi": "MIT", "Jinja2": "BSD-3-Clause"}.get(name, "见许可证原文")
        urls = distribution.metadata.get_all("Project-URL", [])
        source = next((entry.split(", ", 1)[1] for entry in urls
                       if entry.lower().startswith(("source,", "source code,", "repository,", "homepage,"))),
                      f"https://pypi.org/project/{name}/{version}/")
        package_dir = destination / name
        package_dir.mkdir(exist_ok=True)
        for resource in distribution.files or []:
            path = str(resource).lower()
            if ".dist-info/" in path and any(marker in path for marker in ("license", "copying", "notice")):
                resource_path = Path(distribution.locate_file(resource))
                if resource_path.is_file():
                    (package_dir / resource_path.name).write_bytes(resource_path.read_bytes())
        rows.append((name, version, license_name, source))
    for asset in ("bootstrap.min.css", "bootstrap.bundle.min.js", "echarts.min.js"):
        content = (ROOT / "app" / "static" / "vendor" / asset).read_text(encoding="utf-8")
        header = re.search(r"/\*.*?\*/", content, flags=re.S)
        if header:
            (destination / f"{asset}-NOTICE.txt").write_text(header.group(0) + "\n", encoding="utf-8")
    apache = (destination / "freezegun" / "LICENSE").read_text(encoding="utf-8")
    (destination / "Apache-2.0.txt").write_text(apache, encoding="utf-8")
    mit = (destination / "fastapi" / "LICENSE").read_text(encoding="utf-8")
    mit_terms = mit[mit.index("Permission is hereby granted"):]
    (destination / "Bootstrap-MIT.txt").write_text(
        "The MIT License (MIT)\n\nCopyright 2011-2024 The Bootstrap Authors\n\n" + mit_terms,
        encoding="utf-8",
    )
    text = """# 第三方依赖与许可证

作品：社区智慧养老服务与健康监测平台
报名编号：PYTH260036ON；参赛者：唐克波。

## 使用范围

本项目使用下列通用框架和工具完成 Web 接口、数据持久化、模板渲染、加密、调度、图表及测试。
业务实现的位置：app/services、app/algorithms、app/core/state_machine.py；架构和关键算法见 design.md。
本清单标明可核实的第三方组件来源，不替代参赛者对作品独立完成情况的确认。

## Python 直接依赖

版本与 requirements.txt 及打包环境的已安装元数据核对；许可证原文收集在 docs/licenses/。
安装时的传递依赖仍按各自发行包许可证使用，本提交包不分发 Python 安装包或虚拟环境。

| 名称 | 版本 | 许可证 | 来源 |
|---|---|---|---|
"""
    for name, version, license_name, source in rows:
        text += f"| {name} | {version} | {license_name} | [{name}]({source}) |\n"
    text += """
## 随源码分发的前端资源

| 名称 | 版本 | 使用位置 | 许可证与来源 |
|---|---|---|---|
| Bootstrap | 5.3.3 | app/static/vendor/bootstrap.min.css、bootstrap.bundle.min.js | MIT；https://github.com/twbs/bootstrap/tree/v5.3.3 |
| Popper（Bootstrap bundle 内置） | 随 Bootstrap bundle，未单独校验版本 | bootstrap.bundle.min.js 内置定位组件 | MIT；https://github.com/floating-ui/floating-ui |
| Apache ECharts | 5.6.0 | app/static/vendor/echarts.min.js | Apache-2.0；https://github.com/apache/echarts/tree/5.6.0 |

Bootstrap 与 ECharts 版本根据本地资源内容确认；不另行分发独立 Popper 文件。
上游完整声明：Bootstrap https://github.com/twbs/bootstrap/blob/v5.3.3/LICENSE ；
ECharts https://github.com/apache/echarts/blob/5.6.0/LICENSE 和 https://github.com/apache/echarts/blob/5.6.0/NOTICE 。

## 许可证原文

- docs/licenses/ 下保存已安装发行包中的许可证文件、本地前端文件自带声明，以及标准 MIT / Apache 2.0 条款。
- 当前网络下载上游 LICENSE/NOTICE 超时；本目录不将本地资源头部声明冒充上游完整 NOTICE。可按上述固定版本链接核对补充；openpyxl 许可证类型来自已安装元数据，其发行包未附单独许可证文件。
- 文件中的作者、版权与许可证声明保持原文；使用本项目自编业务代码不改变第三方组件的许可证。
- 技术依赖不等于引用同类养老平台核心业务实现；如参赛者另有代码、图片、音视频素材来源，应在提交前继续补充。
"""
    (ROOT / "docs" / "第三方依赖与许可证.md").write_text(text, encoding="utf-8")
    print(f"Collected notices for {len(rows)} direct Python dependencies and bundled frontend assets.")


if __name__ == "__main__":
    main()
