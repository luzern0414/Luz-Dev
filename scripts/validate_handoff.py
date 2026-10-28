#!/usr/bin/env python3
"""Validate a Luz Design HANDOFF.json against the paired schema and local references."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "contracts" / "handoff.schema.json"


def _has_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_schema(value: Any, schema: Mapping[str, Any], at: str = "$") -> list[str]:
    """Validate the JSON Schema keywords used by the Luz handoff contract."""
    errors: list[str] = []
    expected = schema.get("type")
    if isinstance(expected, str) and not _has_type(value, expected):
        return [f"{at}: expected {expected}, got {type(value).__name__}"]

    if "const" in schema and value != schema["const"]:
        errors.append(f"{at}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{at}: expected one of {schema['enum']}")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{at}: string is shorter than {schema['minLength']}")
        if pattern := schema.get("pattern"):
            if re.fullmatch(pattern, value) is None:
                errors.append(f"{at}: does not match {pattern}")

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{at}: needs at least {schema['minItems']} item(s)")
        if schema.get("uniqueItems"):
            normalized = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in value}
            if len(normalized) != len(value):
                errors.append(f"{at}: items must be unique")
        if item_schema := schema.get("items"):
            for index, item in enumerate(value):
                errors.extend(validate_schema(item, item_schema, f"{at}[{index}]"))

    if isinstance(value, dict):
        required = set(schema.get("required", []))
        for key in sorted(required - set(value)):
            errors.append(f"{at}: missing required property {key!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in sorted(set(value) - set(properties)):
                errors.append(f"{at}: unexpected property {key!r}")
        for key, child_schema in properties.items():
            if key in value:
                errors.extend(validate_schema(value[key], child_schema, f"{at}.{key}"))
    return errors


def _validate_local_reference(base: Path, raw_path: Any, label: str) -> list[str]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return [f"{label}: path must be a non-empty string"]
    candidate = (base / raw_path).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return [f"{label}: path escapes the handoff directory: {raw_path}"]
    if not candidate.is_file():
        return [f"{label}: missing local file: {raw_path}"]
    return []


def cross_validate(data: Mapping[str, Any], manifest: Path) -> list[str]:
    """Check local screen and asset references after schema validation succeeds."""
    base = manifest.resolve().parent
    errors: list[str] = []
    for index, asset in enumerate(data["assets"]):
        errors.extend(_validate_local_reference(base, asset["path"], f"assets[{index}] ({asset['id']}) path"))
    return errors


def validate_handoff(
    data: Any,
    manifest_path: str | Path,
    schema: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return all schema and local-reference errors for a parsed handoff."""
    active_schema = schema
    if active_schema is None:
        active_schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = validate_schema(data, active_schema)
    if errors or not isinstance(data, dict):
        return errors
    return cross_validate(data, Path(manifest_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="Path to HANDOFF.json")
    args = parser.parse_args(argv)
    manifest = args.manifest.resolve()
    if not manifest.is_file():
        print(f"FAIL: manifest not found: {manifest}")
        return 1
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: cannot read JSON: {exc}")
        return 1

    errors = validate_handoff(data, manifest, schema)
    if errors:
        print(f"FAIL: {len(errors)} handoff issue(s)")
        for error in errors:
            print("ERROR", error)
        return 1
    print(f"PASS: design handoff is valid: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
