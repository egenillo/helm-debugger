"""
Error classification, risk levels and fix suggestions.
Shared by every output format so text, JSON and SARIF always agree.
"""

import re
from typing import Optional

# Substrings that only appear in YAML (not Go template) errors.
# Note: a bare "yaml:" is NOT used - it matches file names like "cm.yaml:11".
YAML_INDICATORS = (
    "yaml parse error",
    "error converting yaml to json",
    "yaml: line",
    "yaml: unmarshal",
    "yaml: did not find",
    "yaml: mapping values",
    "yaml: could not find",
    "did not find expected",
    "mapping values are not allowed",
    "could not find expected",
    "found character that cannot start",
)

CATEGORY_DESCRIPTIONS = {
    "yaml_structure": "Rendered YAML has an invalid structure (indentation, list items, mappings)",
    "yaml_syntax": "Rendered output is not valid YAML",
    "nil_reference": "A value used by the template is missing (nil pointer)",
    "template_syntax": "Go template syntax error",
    "template_reference": "Reference to an undefined function, template or variable",
    "required_value": "A required value is missing or a fail/required call fired",
    "type_error": "A value has the wrong type for the operation",
    "other": "Other Helm rendering error",
}

RISK_BY_CATEGORY = {
    "nil_reference": "high",
    "template_reference": "high",
    "required_value": "high",
    "yaml_structure": "medium",
    "yaml_syntax": "medium",
    "type_error": "medium",
    "template_syntax": "low",
    "other": "low",
}


def is_yaml_error(error_message: str) -> bool:
    """Check if the error is a YAML parsing error (not a Go template error)."""
    low = (error_message or "").lower()
    return any(indicator in low for indicator in YAML_INDICATORS)


def classify(error_message: str) -> str:
    """Classify an error into a machine-readable category."""
    if not error_message:
        return "other"
    low = error_message.lower()
    if any(k in low for k in ("did not find expected", "mapping values", "could not find expected")):
        return "yaml_structure"
    if is_yaml_error(low):
        return "yaml_syntax"
    if "nil pointer" in low:
        return "nil_reference"
    if "parse error" in low or "unexpected" in low or "unclosed action" in low:
        return "template_syntax"
    if any(k in low for k in ("not defined", "undefined", "no template", "can't evaluate field")):
        return "template_reference"
    if "execution error" in low or "is required" in low:
        return "required_value"
    if any(k in low for k in ("cannot range", "wrong type", "can't handle", "incompatible types",
                              "invalid type", "cannot index")):
        return "type_error"
    return "other"


def risk_level(category: str) -> str:
    """Risk annotation for a category: high / medium / low."""
    return RISK_BY_CATEGORY.get(category, "low")


def suggestions_for(error_message: str) -> list[str]:
    """Actionable fix suggestions based on the Helm error text."""
    low = (error_message or "").lower()
    s: list[str] = []

    if is_yaml_error(low):
        if "indent" in low:
            s.append("Check indentation - YAML requires consistent spacing")
        if "did not find expected" in low or "could not find expected" in low:
            s.append("Check YAML structure - possibly wrong indentation or missing item")
        if "mapping values" in low:
            s.append("Check YAML key-value syntax (key: value) - a value may contain an unquoted ':'")
        if "found character that cannot start" in low:
            s.append("Quote values that start with special characters like @, `, %, or *")
        s.append("Check nindent/indent values on toYaml/include output")

    if "unexpected eof" in low or "unclosed action" in low:
        s.append("A {{ if }}, {{ range }}, {{ with }} or {{ define }} is missing its {{ end }}")
    elif "unexpected {{end}}" in low:
        s.append("There is an extra {{ end }} without a matching opener")
    elif "unexpected" in low and "}" in low:
        s.append("Check for mismatched or missing braces {{ }}")

    if "function" in low and "not defined" in low:
        s.append("The function does not exist in Helm/Sprig - check its spelling")
    elif "no template" in low or ("not defined" in low and "template" in low):
        s.append("The referenced template or helper may not exist - check the define name")
    elif "undefined" in low:
        s.append("Verify the variable exists in values.yaml")
        s.append("Check for typos in variable names")

    if "can't evaluate field" in low:
        s.append("Inside range/with the context (.) changes - use $.Values to reach top-level values")

    if "cannot range over" in low:
        s.append("Ensure the value is a list or map before ranging")

    if "nil pointer" in low:
        s.append("Add a nil check: {{- if .Values.something }} or use `default`/`dig`")

    if "execution error" in low or "is required" in low:
        s.append("A `required` or `fail` call fired - provide the value with -f or --set")

    if "wrong type" in low:
        s.append("Convert the value explicitly, e.g. with toString, int or quote")

    # de-duplicate, keep order
    return list(dict.fromkeys(s))


def extract_file_section(rendered_output: str, rel_path: str) -> Optional[str]:
    """
    Extract one template's rendered YAML from combined `helm template` output.
    `rel_path` is relative to the root chart, e.g. "templates/cm.yaml".
    """
    if not rendered_output:
        return None

    # helm template separates files with:
    #   ---
    #   # Source: <chart>/templates/<file>
    header = re.compile(r'#\s*Source:\s*[^/\s]+/' + re.escape(rel_path) + r'\s*$', re.MULTILINE)
    sections = re.split(r'^---\s*$', rendered_output, flags=re.MULTILINE)

    for section in sections:
        match = header.search(section)
        if match:
            content = section[match.end():].lstrip("\r\n")
            return content if content.strip() else section.strip()

    return None


def rendered_tail(rendered_output: str, rel_path: str, count: int = 5) -> list[tuple[int, str]]:
    """Last `count` rendered lines of a template, as (line_number, content) pairs."""
    section = extract_file_section(rendered_output, rel_path)
    if section is None:
        return []
    lines = section.splitlines()
    start = max(0, len(lines) - count)
    return [(start + i + 1, line) for i, line in enumerate(lines[start:])]
