"""
Helm command execution.
Runs `helm template` and manages a scratch copy of the chart for incremental testing.
"""

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .utils import read_file_content, write_file_content

# A template path as Helm prints it: "<chart>/templates/x.yaml" or
# "<chart>/charts/<sub>/templates/x.yaml". The leading chart name is group 2's prefix.
TEMPLATE_PATH_RE = re.compile(
    r'(?:^|[\s"(<])((?:[\w.-]+)((?:/charts/[\w.-]+)*/templates/[^\s:)"]+))'
)


@dataclass
class HelmResult:
    """Result of a helm template execution."""
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    command: list[str] = field(default_factory=list)

    @property
    def error_message(self) -> str:
        """Extract the main error message from stderr."""
        if not self.stderr:
            return ""

        lines = self.stderr.strip().splitlines()
        # Filter out debug info, keep error lines
        error_lines = [l for l in lines if "Error:" in l or "error:" in l or "template:" in l]
        if error_lines:
            return "\n".join(error_lines)
        return self.stderr.strip()

    def _first_template_path(self) -> Optional[re.Match]:
        if not self.stderr:
            return None
        return TEMPLATE_PATH_RE.search(self.stderr)

    def get_failing_rel_path(self) -> Optional[str]:
        """
        Path of the failing template relative to the root chart,
        e.g. "templates/sub/svc.yaml" or "charts/redis/templates/svc.yaml".
        """
        match = self._first_template_path()
        if not match:
            return None
        return match.group(2).lstrip("/")

    def get_failing_file(self) -> Optional[str]:
        """Basename of the failing template (kept for backwards compatibility)."""
        rel = self.get_failing_rel_path()
        return rel.rsplit("/", 1)[-1] if rel else None

    def get_source_line(self) -> Optional[int]:
        """Template source line reported by Go template errors ("cm.yaml:11:15")."""
        match = self._first_template_path()
        if not match:
            return None
        line = re.search(re.escape(match.group(1)) + r':(\d+)', self.stderr)
        return int(line.group(1)) if line else None

    def get_rendered_line(self) -> Optional[int]:
        """Line in the *rendered* manifest reported by YAML errors ("yaml: line 7")."""
        if not self.stderr:
            return None
        match = re.search(r'yaml: line (\d+)', self.stderr)
        return int(match.group(1)) if match else None

    def get_failing_line(self) -> Optional[int]:
        """Best line number Helm reports (source line first, then rendered line)."""
        return self.get_source_line() or self.get_rendered_line()


class HelmRunner:
    """Builds and runs `helm template` commands."""

    def __init__(
        self,
        helm_path: str,
        release_name: str = "debug-release",
        values_files: Optional[list[str]] = None,
        set_values: Optional[list[str]] = None,
        extra_args: Optional[list[str]] = None,
        timeout: int = 60,
    ):
        self.helm_path = helm_path
        self.release_name = release_name
        # Values files are resolved now: helm runs against a temp copy of the chart
        self.values_files = [self._resolve(v) for v in (values_files or [])]
        self.set_values = set_values or []
        self.extra_args = extra_args or []
        self.timeout = timeout

    @staticmethod
    def _resolve(values_file: str) -> str:
        if re.match(r'^[a-z][a-z0-9+.-]*://', values_file) or values_file == "-":
            return values_file  # URL or stdin
        return str(Path(values_file).resolve())

    def build_command(self, chart_path: str) -> list[str]:
        cmd = [self.helm_path, "template", self.release_name, chart_path]
        for values_file in self.values_files:
            cmd.extend(["-f", values_file])
        for set_val in self.set_values:
            cmd.extend(["--set", set_val])
        cmd.extend(self.extra_args)
        return cmd

    def run(self, chart_path: str) -> HelmResult:
        """Run helm template on a chart directory."""
        return self._exec(self.build_command(chart_path))

    def dependency_update(self, chart_path: str) -> HelmResult:
        return self._exec([self.helm_path, "dependency", "update", chart_path])

    def _exec(self, cmd: list[str]) -> HelmResult:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
            return HelmResult(
                success=result.returncode == 0,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                command=cmd,
            )
        except subprocess.TimeoutExpired:
            return HelmResult(False, "", f"Command timed out after {self.timeout} seconds", -1, cmd)
        except OSError as e:
            return HelmResult(False, "", str(e), -1, cmd)


class ChartWorkspace:
    """
    A scratch copy of the chart, created once per debugging session.
    Each render() swaps in modified template content, runs helm, then restores it,
    so the (possibly large) chart is copied only once and the original is never touched.
    """

    def __init__(self, chart_path: str, runner: HelmRunner, dependency_update: bool = False):
        self.original_path = Path(chart_path).resolve()
        self.runner = runner
        self.dependency_update = dependency_update
        self.temp_dir: Optional[Path] = None
        self.chart_copy: Optional[Path] = None
        self.render_count = 0

    def __enter__(self) -> "ChartWorkspace":
        self.temp_dir = Path(tempfile.mkdtemp(prefix="helm_debug_"))
        self.chart_copy = self.temp_dir / self.original_path.name
        shutil.copytree(
            self.original_path,
            self.chart_copy,
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )
        if self.dependency_update:
            result = self.runner.dependency_update(str(self.chart_copy))
            if not result.success:
                self.cleanup()
                raise RuntimeError(f"helm dependency update failed:\n{result.stderr.strip()}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()

    def cleanup(self):
        if self.temp_dir and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir = None
        self.chart_copy = None

    def render(self, rel_path: Optional[str] = None, content: Optional[str] = None) -> HelmResult:
        """Render the chart, optionally with one template replaced by `content`."""
        if self.chart_copy is None:
            raise RuntimeError("ChartWorkspace not initialized. Use it as a context manager.")

        self.render_count += 1
        if rel_path is None or content is None:
            return self.runner.run(str(self.chart_copy))

        target = self.chart_copy / rel_path
        original = read_file_content(target)
        write_file_content(target, content)
        try:
            return self.runner.run(str(self.chart_copy))
        finally:
            write_file_content(target, original)
