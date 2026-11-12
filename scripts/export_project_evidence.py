"""Export a bounded, visible project conversation without private reasoning.

Only an explicitly selected JSONL range is exported. The session metadata must
name this project or its parent directory. System/developer messages, reasoning,
environment injection, tool descriptions, and image/audio data are excluded.
The result is a filtered evidence copy, never a claim to be a raw full export.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def export_evidence(source: Path, start: int, end: int, output: Path) -> dict[str, Any]:
    source = source.resolve()
    raw = source.read_bytes()
    lines = raw.splitlines()
    metadata = json.loads(lines[0]).get("payload", {})
    cwd = Path(metadata.get("cwd", "")).resolve()
    if cwd not in {ROOT.resolve(), ROOT.parent.resolve()}:
        raise ValueError("Session cwd is outside the project and its parent")
    if not 1 <= start <= end <= len(lines):
        raise ValueError("Invalid source line range")
    records: list[dict[str, Any]] = []
    exclusions: Counter[str] = Counter()
    redactions: Counter[str] = Counter()

    def clean(text: str) -> str:
        patterns = {
            "jwt": r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b",
            "api_key": r"\b(?:sk|ghp|github_pat)[-_][A-Za-z0-9_-]{20,}\b",
            "private_key": r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----",
            "unavailable_encrypted_text": r"\bgAAAA[A-Za-z0-9_=-]{60,}",
        }
        for name, pattern in patterns.items():
            text, count = re.subn(pattern, "[REDACTED:" + name + "]", text)
            redactions[name] += count
        return text

    def content_text(value: Any) -> str:
        if isinstance(value, str):
            return clean(value)
        if isinstance(value, list):
            result = []
            for item in value:
                if isinstance(item, dict) and item.get("type") in {"input_text", "output_text", "text"}:
                    result.append(clean(item.get("text", "")))
                else:
                    exclusions["non_text_content"] += 1
            return "\n".join(result)
        return clean(json.dumps(value, ensure_ascii=False))

    for number in range(start, end + 1):
        row = json.loads(lines[number - 1])
        if row.get("type") != "response_item":
            exclusions[row.get("type", "unknown")] += 1
            continue
        payload = row.get("payload", {})
        kind = payload.get("type")
        base = {
            "timestamp": row.get("timestamp"), "source_line": number,
            "source_record_sha256": digest(lines[number - 1]), "type": kind,
        }
        if kind == "message" and payload.get("role") in {"user", "assistant"}:
            text = content_text(payload.get("content", []))
            if text.lstrip().startswith("<environment_context>"):
                exclusions["environment_context"] += 1
                continue
            if payload.get("channel") == "analysis" or payload.get("phase") == "analysis":
                exclusions["analysis_message"] += 1
                continue
            base.update(role=payload["role"], phase=payload.get("phase"), text=text)
        elif kind in {"custom_tool_call", "function_call"}:
            base.update(tool=payload.get("name"), call_id=payload.get("call_id"),
                        text=content_text(payload.get("input", payload.get("arguments", ""))))
        elif kind in {"custom_tool_call_output", "function_call_output"}:
            base.update(call_id=payload.get("call_id"), text=content_text(payload.get("output", "")))
        else:
            exclusions[str(kind)] += 1
            continue
        records.append(base)

    output.mkdir(parents=True, exist_ok=True)
    transcript = output / "visible-conversation.jsonl"
    transcript.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    markdown = output / "visible-conversation.md"
    headings = [
        "# 本项目所选会话范围：可见对话及工具证据副本",
        "",
        "这是经筛选的可见记录，不含系统/开发者提示、私有推理、运行时元数据及非文本媒体。",
        "原始用户中文含替换字符时原样保留，不能由后人补写为原始Prompt。",
        "来源行号用于与本地原始JSONL核对；原文件及逐条记录SHA-256见清单。",
        "",
    ]
    for record in records:
        label = record.get("role") or record.get("tool") or record["type"]
        headings.extend([
            f"## L{record['source_line']} | {record['timestamp']} | {label}", "",
            "````text", record["text"], "````", "",
        ])
    markdown.write_text("\n".join(headings), encoding="utf-8")
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_path": str(source), "source_sha256": digest(raw),
        "source_bytes": len(raw), "source_cwd": str(cwd),
        "source_session_id": metadata.get("id"),
        "selected_lines": [start, end], "exported_records": len(records),
        "record_types": dict(Counter(r["type"] for r in records)),
        "user_messages": sum(r.get("role") == "user" for r in records),
        "source_replacement_characters": sum(r.get("text", "").count("\ufffd") for r in records),
        "exclusions": dict(exclusions), "redactions": dict(redactions),
        "files": {p.name: {"sha256": digest(p.read_bytes()), "bytes": p.stat().st_size}
                  for p in (transcript, markdown)},
        "limitations": [
            "Filtered visible copy; original raw session is not bundled.",
            "Subject indexes or agent delegated tasks do not prove independent human prompt chains.",
            "Replacement characters, if present in source text, are retained without reconstruction.",
            "Transcript evidence does not create missing fine-grained Git history.",
            "Encrypted inter-agent text cannot be recovered and is omitted, never reconstructed as raw evidence.",
        ],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--start-line", type=int, required=True)
    parser.add_argument("--end-line", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = export_evidence(args.source, args.start_line, args.end_line, args.output)
    print(json.dumps({k: result[k] for k in ("selected_lines", "exported_records", "user_messages", "redactions")}, ensure_ascii=True))


if __name__ == "__main__":
    main()
