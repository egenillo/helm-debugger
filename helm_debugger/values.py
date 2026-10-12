"""
Values diagnostics.

When a template fails on a `.Values.x.y.z` expression (nil pointer, wrong type),
reconstruct the merged values the way Helm does (chart defaults, -f files, --set,
subchart scoping) and explain which key is missing - with "did you mean" hints.
PyYAML is optional: without it, diagnostics are simply skipped.
"""

import difflib
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

from .utils import read_file_content

try:  # optional dependency
    import yaml
except ImportError:  # pragma: no cover - exercised only without PyYAML
    yaml = None

VALUES_EXPR_RE = re.compile(r'at <\$?\.Values((?:\.[A-Za-z0-9_-]+)+)>')


@dataclass
class ValuesDiagnostic:
    """Explains why a `.Values` expression could not be evaluated."""
    expression: str                  # ".Values.image.tagg.x"
    problem: str                     # "missing" | "null" | "not_a_map"
    path: str                        # first problematic path, e.g. ".Values.image.tagg"
    parent: str                      # ".Values.image"
    available_keys: list[str] = field(default_factory=list)
    suggestion: Optional[str] = None  # ".Values.image.tag"
    value_type: Optional[str] = None  # type of the value when problem == "not_a_map"
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def yaml_available() -> bool:
    return yaml is not None


def extract_values_expression(error_message: str) -> Optional[list[str]]:
    """Return the key path of the failing `.Values` expression, e.g. ["image", "tagg", "x"]."""
    match = VALUES_EXPR_RE.search(error_message or "")
    if not match:
        return None
    return match.group(1).lstrip(".").split(".")


def deep_merge(base: dict, override: dict) -> dict:
    """Merge `override` into a copy of `base` (maps merge recursively, null deletes)."""
    result = dict(base)
    for key, value in override.items():
        if value is None and key in result:
            del result[key]
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _split_unescaped(text: str, sep: str) -> list[str]:
    parts = re.split(r'(?<!\\)' + re.escape(sep), text)
    return [p.replace("\\" + sep, sep) for p in parts]


def _typed(value: str) -> Any:
    if value in ("true", "false"):
        return value == "true"
    if value == "null":
        return None
    if re.fullmatch(r'-?\d+', value):
        return int(value)
    return value


def parse_set_values(set_values: list[str]) -> dict:
    """Parse --set arguments (a.b=c,d[0]=e) into a nested dict. Best effort."""
    result: dict = {}
    for arg in set_values:
        for assignment in _split_unescaped(arg, ","):
            if "=" not in assignment:
                continue
            key, value = assignment.split("=", 1)
            node: Any = result
            parts = _split_unescaped(key, ".")
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                index_match = re.fullmatch(r'(.+)\[(\d+)\]', part)
                if index_match:
                    name, idx = index_match.group(1), int(index_match.group(2))
                    lst = node.setdefault(name, [])
                    if not isinstance(lst, list):
                        break
                    while len(lst) <= idx:
                        lst.append(None)
                    if last:
                        lst[idx] = _typed(value)
                    else:
                        if not isinstance(lst[idx], dict):
                            lst[idx] = {}
                        node = lst[idx]
                elif last:
                    node[part] = _typed(value)
                else:
                    if not isinstance(node.get(part), dict):
                        node[part] = {}
                    node = node[part]
    return result


def _load_yaml_file(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = yaml.safe_load(read_file_content(path))
    return data if isinstance(data, dict) else {}


def load_merged_values(
    chart_dir: Path,
    template_rel_path: str,
    values_files: list[str],
    set_values: list[str],
) -> Optional[dict]:
    """
    Build the `.Values` a template sees: root defaults + -f files + --set,
    then scoped down to the subchart that owns `template_rel_path`.
    Returns None if PyYAML is unavailable or a file can't be parsed.
    """
    if yaml is None:
        return None
    try:
        values = _load_yaml_file(chart_dir / "values.yaml")
        for values_file in values_files:
            if "://" in values_file or values_file == "-":
                continue
            values = deep_merge(values, _load_yaml_file(Path(values_file)))
        values = deep_merge(values, parse_set_values(set_values))

        # Scope down into subcharts: charts/<a>/charts/<b>/templates/x.yaml -> [a, b]
        current_dir = chart_dir
        for sub in re.findall(r'charts/([^/]+)/', template_rel_path):
            current_dir = current_dir / "charts" / sub
            parent_global = values.get("global", {}) if isinstance(values.get("global"), dict) else {}
            own = values.get(sub, {}) if isinstance(values.get(sub), dict) else {}
            values = deep_merge(_load_yaml_file(current_dir / "values.yaml"), own)
            merged_global = deep_merge(values.get("global", {}) or {}, parent_global)
            if merged_global:
                values["global"] = merged_global
        return values
    except (yaml.YAMLError, OSError, ValueError):
        return None


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    return type(value).__name__


def diagnose_path(values: dict, keys: list[str]) -> Optional[ValuesDiagnostic]:
    """Walk `keys` through `values` and explain the first point where it breaks."""
    expression = ".Values." + ".".join(keys)
    current: Any = values
    walked: list[str] = []

    for key in keys:
        parent = ".Values" + "".join("." + k for k in walked)
        path = parent + "." + key

        if current is None:
            return ValuesDiagnostic(
                expression, "null", parent, ".Values" + "".join("." + k for k in walked[:-1]),
                message=f"{parent} is null, so {path} cannot be read. "
                        f"Set {parent} in your values or guard it with {{{{- if {parent} }}}}.",
            )

        if not isinstance(current, dict):
            kind = _type_name(current)
            return ValuesDiagnostic(
                expression, "not_a_map", parent, ".Values" + "".join("." + k for k in walked[:-1]),
                value_type=kind,
                message=f"{parent} is a {kind}, not a map, so it has no key '{key}'.",
            )

        if key not in current:
            available = sorted(str(k) for k in current.keys())
            close = difflib.get_close_matches(key, available, n=1, cutoff=0.6)
            suggestion = parent + "." + close[0] if close else None
            message = f"{path} is not defined"
            if available:
                shown = ", ".join(available[:10]) + (", ..." if len(available) > 10 else "")
                message += f" ({parent} has: {shown})"
            else:
                message += f" ({parent} is empty)"
            message += "."
            if suggestion:
                message += f" Did you mean {suggestion}?"
            return ValuesDiagnostic(
                expression, "missing", path, parent, available, suggestion, message=message,
            )

        walked.append(key)
        current = current[key]

    return None  # the full path resolves; the problem is elsewhere


def diagnose(
    error_message: str,
    chart_dir: Path,
    template_rel_path: str,
    values_files: list[str],
    set_values: list[str],
) -> Optional[ValuesDiagnostic]:
    """Top-level entry point: diagnose the .Values expression in a Helm error, if any."""
    keys = extract_values_expression(error_message)
    if not keys:
        return None
    values = load_merged_values(chart_dir, template_rel_path, values_files, set_values)
    if values is None:
        return None
    return diagnose_path(values, keys)
