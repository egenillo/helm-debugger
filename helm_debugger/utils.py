"""
Utility functions for Helm template debugger.
Handles chart discovery, file I/O and helper functions.
"""

import os
import re
import shutil
from pathlib import Path
from typing import Optional

TEMPLATE_SUFFIXES = (".yaml", ".yml", ".tpl")


def find_helm_executable() -> Optional[str]:
    """
    Find the helm executable.
    HELM_BIN is honoured first: Helm sets it when running us as a plugin.
    """
    helm_bin = os.environ.get("HELM_BIN")
    if helm_bin and Path(helm_bin).exists():
        return helm_bin

    found = shutil.which("helm")
    if found:
        return found

    # Try common locations
    common_paths = [
        "/usr/local/bin/helm",
        "/usr/bin/helm",
        "/opt/homebrew/bin/helm",
        "C:\\Program Files\\Helm\\helm.exe",
        "C:\\ProgramData\\chocolatey\\bin\\helm.exe",
    ]

    for path in common_paths:
        if Path(path).exists():
            return path

    return None


def find_chart_yaml(chart_dir: Path) -> Optional[Path]:
    """Return the Chart.yaml (or Chart.yml) of a chart directory, if any."""
    for name in ("Chart.yaml", "Chart.yml"):
        candidate = chart_dir / name
        if candidate.is_file():
            return candidate
    return None


def validate_chart_directory(chart_path: str) -> tuple[bool, str]:
    """
    Validate that the given path is a valid Helm chart.
    Returns (is_valid, error_message).
    """
    path = Path(chart_path)

    if not path.exists():
        return False, f"Chart path does not exist: {chart_path}"

    if not path.is_dir():
        return False, f"Chart path is not a directory: {chart_path}"

    if not find_chart_yaml(path):
        return False, f"No Chart.yaml found in: {chart_path}"

    templates_dir = path / "templates"
    if not templates_dir.exists():
        return False, f"No templates directory found in: {chart_path}"

    return True, ""


def read_chart_name(chart_dir: Path) -> str:
    """Read the chart name from Chart.yaml (falls back to the directory name)."""
    chart_yaml = find_chart_yaml(chart_dir)
    if chart_yaml:
        match = re.search(r'^name:\s*["\']?([^"\'\s#]+)', read_file_content(chart_yaml), re.MULTILINE)
        if match:
            return match.group(1)
    return chart_dir.name


def iter_chart_dirs(chart_path: Path):
    """
    Yield (chart_dir, rel_prefix) for a chart and its unpacked subcharts.
    rel_prefix is the chart's location relative to the root chart, e.g. "" or "charts/redis/".
    Packaged (.tgz) subcharts are skipped: their templates are not editable.
    """
    yield chart_path, ""
    charts_dir = chart_path / "charts"
    if not charts_dir.is_dir():
        return
    for sub in sorted(charts_dir.iterdir()):
        if sub.is_dir() and find_chart_yaml(sub):
            for sub_dir, sub_prefix in iter_chart_dirs(sub):
                yield sub_dir, f"charts/{sub.name}/{sub_prefix}"


def get_template_files(chart_path: str) -> list[tuple[Path, str]]:
    """
    Get all template files of a chart and its unpacked subcharts.
    Returns (absolute_path, rel_path) pairs; rel_path is POSIX-style and relative
    to the root chart, e.g. "templates/deployment.yaml" or "charts/redis/templates/svc.yaml".
    """
    root = Path(chart_path)
    template_files = []

    for chart_dir, prefix in iter_chart_dirs(root):
        templates_dir = chart_dir / "templates"
        if not templates_dir.is_dir():
            continue
        for file_path in templates_dir.rglob("*"):
            if file_path.is_file() and file_path.suffix in TEMPLATE_SUFFIXES:
                rel = file_path.relative_to(chart_dir).as_posix()
                template_files.append((file_path, prefix + rel))

    return sorted(template_files, key=lambda item: item[1])


def read_file_content(file_path: Path) -> str:
    """Read file content with proper encoding handling."""
    encodings = ["utf-8", "utf-8-sig", "latin-1"]

    for encoding in encodings:
        try:
            return file_path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue

    raise ValueError(f"Could not read file with any supported encoding: {file_path}")


def write_file_content(file_path: Path, content: str):
    """Write content to file with UTF-8 encoding (no newline translation)."""
    with open(file_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
