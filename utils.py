"""
Utility functions for Helm template debugger.
Handles temp directory management and helper functions.
"""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional


class TempChartManager:
    """Manages temporary chart copies for incremental testing."""

    def __init__(self, original_chart_path: str):
        self.original_path = Path(original_chart_path).resolve()
        self.temp_dir: Optional[Path] = None

    def __enter__(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="helm_debug_"))
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()

    def cleanup(self):
        """Remove temporary directory."""
        if self.temp_dir and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)
            self.temp_dir = None

    def create_chart_copy(self) -> Path:
        """Create a full copy of the chart in temp directory."""
        if not self.temp_dir:
            raise RuntimeError("TempChartManager not initialized. Use with context manager.")

        chart_copy = self.temp_dir / "chart"
        shutil.copytree(self.original_path, chart_copy)
        return chart_copy

    def get_temp_dir(self) -> Path:
        """Get the temp directory path."""
        if not self.temp_dir:
            raise RuntimeError("TempChartManager not initialized.")
        return self.temp_dir


def find_helm_executable() -> Optional[str]:
    """Find the helm executable in PATH."""
    helm_cmd = "helm.exe" if os.name == "nt" else "helm"

    # Check if helm is in PATH
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    for directory in path_dirs:
        helm_path = Path(directory) / helm_cmd
        if helm_path.exists():
            return str(helm_path)

    # Try common locations
    common_paths = [
        "/usr/local/bin/helm",
        "/usr/bin/helm",
        "C:\\Program Files\\Helm\\helm.exe",
        "C:\\ProgramData\\chocolatey\\bin\\helm.exe",
    ]

    for path in common_paths:
        if Path(path).exists():
            return path

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

    chart_yaml = path / "Chart.yaml"
    if not chart_yaml.exists():
        chart_yaml = path / "Chart.yml"
        if not chart_yaml.exists():
            return False, f"No Chart.yaml found in: {chart_path}"

    templates_dir = path / "templates"
    if not templates_dir.exists():
        return False, f"No templates directory found in: {chart_path}"

    return True, ""


def get_template_files(chart_path: str) -> list[Path]:
    """Get all template files from a chart's templates directory."""
    templates_dir = Path(chart_path) / "templates"
    template_files = []

    if not templates_dir.exists():
        return template_files

    for file_path in templates_dir.rglob("*"):
        if file_path.is_file() and file_path.suffix in (".yaml", ".yml", ".tpl"):
            template_files.append(file_path)

    return sorted(template_files)


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
    """Write content to file with UTF-8 encoding."""
    file_path.write_text(content, encoding="utf-8")
