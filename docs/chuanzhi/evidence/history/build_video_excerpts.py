"""Build attributed presentation excerpts from the filtered visible evidence."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "2026-11-12-optimization" / "visible-conversation.jsonl"
records = {r["source_line"]: r for r in map(json.loads, SOURCE.read_text(encoding="utf-8").splitlines())}


def outputs(value: object) -> list[str]:
    if isinstance(value, dict):
        found = [value["output"]] if isinstance(value.get("output"), str) else []
        for child in value.values():
            if isinstance(child, (list, dict)):
                found.extend(outputs(child))
        return found
    if isinstance(value, list):
        return [text for child in value for text in outputs(child)]
    return []


def patch(line: int, needle: str, count: int = 14) -> str:
    match = re.search(r"tools\.apply_patch\((\"(?:[^\"\\]|\\.)*\")\)", records[line]["text"])
    if not match:
        raise ValueError("Expected literal patch")
    lines = json.loads(match.group(1)).splitlines()
    start = next(i for i, text in enumerate(lines) if needle in text)
    return "\n".join(lines[start:start + count])


def matching_output(line: int, pattern: str) -> str:
    chunks = []
    for text in records[line]["text"].splitlines():
        try:
            chunks.extend(outputs(json.loads(text)))
        except json.JSONDecodeError:
            pass
    selected = [text for chunk in chunks for text in chunk.splitlines() if re.search(pattern, text)]
    if not selected:
        raise ValueError(f"No matching output in L{line}")
    return "\n".join(selected)


excerpts = [
    ("周期预约：明确事务边界", 173, "将工具输入中的JSON字符串解码为原补丁文本，截取连续行。",
     patch(173, "with db.begin_nested():", 14)),
    ("派单：助手说明约束缺口", 114, "助手可见回复原文，未改写。", records[114]["text"]),
    ("真实失败：旧超时语义与负荷测试未通过", 185, "从工具输出JSON解码后筛选含FAILED的原始行。",
     matching_output(185, r"^FAILED")),
    ("修复说明：Windows SQLite连接占用", 477, "助手可见回复原文，未改写。", records[477]["text"]),
    ("最终验证：410项通过", 496, "从工具输出JSON解码后筛选测试与覆盖率结果原始行。",
     matching_output(496, r"410 passed|89\.14|failures|errors|testsuite|tests=|failures=|errors=")),
]
result = []
for title, line, transform, text in excerpts:
    result.append({
        "title": title, "source_line": line, "timestamp": records[line]["timestamp"],
        "source_record_sha256": records[line]["source_record_sha256"],
        "presentation_transformation": transform, "text": text,
    })
target = HERE / "video-excerpts.json"
target.write_text(json.dumps({
    "notice": "真实可见证据摘录。属于同一个用户发起的优化轮；不能当成3条独立完整Prompt链。",
    "source_visible_copy": str(SOURCE.relative_to(HERE)),
    "source_visible_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
    "excerpts": result,
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(target)
