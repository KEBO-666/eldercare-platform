"""Verify immutable selected source records without printing sensitive content."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANCHORS = {
    "appointment": {1470: "时间冲突、周期预约、取消后的时段释放", 1850: "正常与异常场景都有清楚反馈"},
    "dispatch": {2210: "候选排序、权重解释、无人可派时的处理", 2769: "页面解释与实际计算一致"},
    "health-alert": {3167: "异常上报、响应、处置、闭环追踪", 3825: "确保权限正确、状态完整、操作可追溯", 4418: "继续"},
    "role-ui": {4532: "出几套供我选择", 4626: "按推荐组合", 5146: "还差啥", 5182: "不是已经做过了吗"},
    "sync-request": {5202: "同步完整对话证据、更新索引"},
}
SENSITIVE = {
    "jwt": r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b",
    "provider_token": r"\b(?:sk-|ghp_|github_pat_|gh[ousr]_)[A-Za-z0-9_-]{20,}\b",
    "private_key": r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----",
    "encrypted_body": r"\bgAAAA[A-Za-z0-9_=-]{60,}",
    "authorization_literal": r"(?i)\b(?:Bearer|Basic) [A-Za-z0-9_+/.=-]{24,}",
    "credential_url": r"(?:https?|postgres(?:ql)?(?:\+[A-Za-z0-9]+)?|mysql)://[A-Za-z0-9_.%+-]+:[A-Za-z0-9_.%+!~*-]+@",
}
checks = []
for name, anchors in ANCHORS.items():
    folder = HERE / name
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    source = Path(manifest["source_path"]).read_bytes()[:manifest["source_bytes"]]
    assert hashlib.sha256(source).hexdigest() == manifest["source_sha256"], name
    lines = source.splitlines()
    records = [json.loads(line) for line in (folder / "visible-conversation.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == manifest["exported_records"]
    by_line = {record["source_line"]: record for record in records}
    for line, text in anchors.items():
        assert by_line[line].get("role") == "user" and text in by_line[line]["text"], (name, line)
    assert sum(record.get("role") == "user" for record in records) == len(anchors), name
    for record in records:
        raw = lines[record["source_line"] - 1]
        original = json.loads(raw)
        assert hashlib.sha256(raw).hexdigest() == record["source_record_sha256"]
        assert not original.get("metadata", {}).get("compaction_output")
        assert record["type"] in {"message", "custom_tool_call", "custom_tool_call_output", "function_call", "function_call_output"}
        if record["type"] == "message":
            assert record["role"] in {"user", "assistant"} and record.get("phase") != "analysis"
        for label, pattern in SENSITIVE.items():
            assert not re.search(pattern, record["text"]), (name, record["source_line"], label)
        assert "session_index.jsonl" not in record["text"], (name, record["source_line"], "unrelated_thread_inventory")
    for filename, data in manifest["files"].items():
        assert hashlib.sha256((folder / filename).read_bytes()).hexdigest() == data["sha256"]
    checks.append({"chain": name, "records": len(records), "user_prompts": len(anchors),
                   "source_and_export_hashes": "passed", "compaction_and_internal_roles_excluded": True,
                   "sensitive_literal_patterns": "no matches", "exact_user_prompt_anchors": "passed"})
result = {"status": "passed", "cutoff_source_line": 5205, "records": sum(x["records"] for x in checks),
          "user_prompts": sum(x["user_prompts"] for x in checks), "checks": checks,
          "scope": "Verifies exported visible records and lexical sensitive patterns; does not certify contest eligibility or recover excluded encrypted/media content."}
(HERE / "integrity.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, ensure_ascii=True, indent=2))
