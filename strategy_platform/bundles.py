"""Portable strategy-only backups. No account, credential or chat export."""

from copy import deepcopy
import hashlib
import hmac
import json
import re

from strategy_platform.dsl import validate_strategy_spec

FORMAT = "mark-six-strategy-revision"
ENGINE_VERSION = "strategy-platform-v1"
MAX_BUNDLE_BYTES = 100_000
_SECRET = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}|Bearer\s+\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})", re.I)


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


def build_bundle(definition, revision):
    spec = json.loads(revision.spec_json)
    validate_strategy_spec(spec)
    if _SECRET.search(canonical_json(spec)):
        raise ValueError("backup_sensitive_content")
    def public_text(value):
        return _SECRET.sub("[redacted]", str(value or ""))
    payload = {
        "format": FORMAT, "schema_version": 1,
        "strategy": {
            "id": definition.public_id,
            "name": public_text(definition.name),
            "kind": "builtin_config" if spec["kind"] == "builtin_config" else "rule_dsl",
            "revision_no": revision.revision_no,
            "spec_schema_version": revision.schema_version,
            "engine_compatibility": revision.engine_compatibility,
            "change_summary": public_text(revision.change_summary),
            "spec": spec,
        },
    }
    payload["digest"] = _digest(payload)
    return payload


def validate_bundle(bundle):
    if not isinstance(bundle, dict) or set(bundle) != {"format", "schema_version", "strategy", "digest"}:
        raise ValueError("策略包字段不受支持")
    if len(canonical_json(bundle).encode()) > MAX_BUNDLE_BYTES:
        raise ValueError("策略备份文件过大")
    if bundle["format"] != FORMAT or type(bundle["schema_version"]) is not int or bundle["schema_version"] != 1:
        raise ValueError("策略包版本不受支持")
    value = deepcopy(bundle)
    digest = value.pop("digest")
    if not isinstance(digest, str) or not hmac.compare_digest(digest, _digest(value)):
        raise ValueError("策略包摘要不匹配")
    strategy = bundle["strategy"]
    required = {"id", "name", "kind", "revision_no", "spec_schema_version", "engine_compatibility", "change_summary", "spec"}
    if not isinstance(strategy, dict) or set(strategy) != required:
        raise ValueError("策略包包含无效或敏感字段")
    if strategy["engine_compatibility"] != ENGINE_VERSION or type(strategy["spec_schema_version"]) is not int or strategy["spec_schema_version"] != 1:
        raise ValueError("策略包不兼容当前执行引擎")
    if not isinstance(strategy["name"], str) or not 1 <= len(strategy["name"]) <= 120:
        raise ValueError("策略包名称无效")
    if not isinstance(strategy["change_summary"], str) or len(strategy["change_summary"]) > 500:
        raise ValueError("策略包变更摘要无效")
    if type(strategy["revision_no"]) is not int or strategy["revision_no"] < 1:
        raise ValueError("策略包修订号无效")
    validate_strategy_spec(strategy["spec"])
    expected_kind = "builtin_config" if strategy["spec"]["kind"] == "builtin_config" else "rule_dsl"
    if strategy["kind"] != expected_kind or _SECRET.search(canonical_json(strategy)):
        raise ValueError("策略包含有敏感内容或不匹配的策略类型")
    return deepcopy(bundle)
