"""
Helm command executor.
Handles running helm template commands and capturing results.
"""

import re
import subprocess
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from utils import (
    find_helm_executable,
    TempChartManager,
    write_file_content,
    read_file_content
)
from parser import ParsedTemplate, TemplateBlock


@dataclass
class HelmResult:
    """Result of a helm template execution."""
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    command: list[str]

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
        return self.stderr

    def get_failing_file(self) -> Optional[str]:
        """Extract the failing template file from Helm error message."""
        if not self.stderr:
            return None

        # Patterns to match Helm error messages
        patterns = [
            # YAML parse error: "Error: YAML parse error on chart/templates/file.yaml"
            r'YAML parse error on [^/]+/templates/([^:]+)',
            # Template error: "template: chart/templates/file.yaml:line"
            r'template: [^/]+/templates/([^:]+):',
            # Parse error: "parse error at (chart/templates/file.yaml:line)"
            r'parse error at \([^/]+/templates/([^:)]+)',
            # Generic path: "templates/file.yaml"
            r'templates/([^\s:]+\.(?:yaml|yml|tpl))',
        ]

        for pattern in patterns:
            match = re.search(pattern, self.stderr)
            if match:
                return match.group(1)

        return None

    def get_failing_line(self) -> Optional[int]:
        """Extract the failing line number from Helm error message."""
        if not self.stderr:
            return None

        # Patterns to match line numbers
        patterns = [
            # "yaml: line 66:"
            r'yaml: line (\d+)',
            # "file.yaml:66:"
            r'\.yaml:(\d+)',
            r'\.yml:(\d+)',
            r'\.tpl:(\d+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, self.stderr)
            if match:
                return int(match.group(1))

        return None


class HelmExecutor:
    """Executes helm template commands."""

    def __init__(
        self,
        helm_path: Optional[str] = None,
        release_name: str = "debug-release",
        values_files: Optional[list[str]] = None,
        set_values: Optional[list[str]] = None,
        extra_args: Optional[list[str]] = None
    ):
        self.helm_path = helm_path or find_helm_executable()
        if not self.helm_path:
            raise RuntimeError("Helm executable not found. Please install Helm or provide path.")

        self.release_name = release_name
        self.values_files = values_files or []
        self.set_values = set_values or []
        self.extra_args = extra_args or []

    def run_template(self, chart_path: str, timeout: int = 60) -> HelmResult:
        """Run helm template on a chart."""
        cmd = [self.helm_path, "template", self.release_name, chart_path]

        # Add values files
        for values_file in self.values_files:
            cmd.extend(["-f", values_file])

        # Add set values
        for set_val in self.set_values:
            cmd.extend(["--set", set_val])

        # Add extra args
        cmd.extend(self.extra_args)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(Path(chart_path).parent)
            )

            return HelmResult(
                success=result.returncode == 0,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                command=cmd
            )

        except subprocess.TimeoutExpired:
            return HelmResult(
                success=False,
                stdout="",
                stderr=f"Command timed out after {timeout} seconds",
                exit_code=-1,
                command=cmd
            )
        except Exception as e:
            return HelmResult(
                success=False,
                stdout="",
                stderr=str(e),
                exit_code=-1,
                command=cmd
            )


class IncrementalExecutor:
    """Executes templates incrementally for debugging."""

    def __init__(self, executor: HelmExecutor, chart_path: str):
        self.executor = executor
        self.chart_path = Path(chart_path)

    def execute_with_blocks(
        self,
        template: ParsedTemplate,
        blocks_to_include: list[TemplateBlock]
    ) -> HelmResult:
        """
        Execute helm template with only specified blocks included.
        Other content is commented out.
        """
        with TempChartManager(str(self.chart_path)) as temp_manager:
            temp_chart = temp_manager.create_chart_copy()

            # Modify the template file
            self._create_partial_template(temp_chart, template, blocks_to_include)

            # Run helm template
            return self.executor.run_template(str(temp_chart))

    def execute_up_to_block(
        self,
        template: ParsedTemplate,
        block_index: int
    ) -> HelmResult:
        """Execute helm template including blocks 0 to block_index."""
        blocks_to_include = template.blocks[:block_index + 1]
        return self.execute_with_blocks(template, blocks_to_include)

    def execute_up_to_line(
        self,
        template: ParsedTemplate,
        line_number: int
    ) -> HelmResult:
        """Execute helm template including content up to a specific line."""
        blocks_to_include = [
            b for b in template.blocks if b.end_line <= line_number
        ]
        return self.execute_with_blocks(template, blocks_to_include)

    def _create_partial_template(
        self,
        temp_chart: Path,
        template: ParsedTemplate,
        blocks_to_include: list[TemplateBlock]
    ):
        """Create a modified template with only specified blocks."""
        # Get relative path of template within chart
        rel_path = template.file_path.relative_to(self.chart_path)
        target_file = temp_chart / rel_path

        # Build new content from included blocks
        included_lines = set()
        for block in blocks_to_include:
            for line_num in range(block.start_line, block.end_line + 1):
                included_lines.add(line_num)

        # Reconstruct file content
        original_lines = template.original_content.splitlines(keepends=True)
        new_lines = []

        for i, line in enumerate(original_lines):
            line_num = i + 1
            if line_num in included_lines:
                new_lines.append(line)
            else:
                # Comment out the line using Go template comment
                stripped = line.rstrip('\n\r')
                if stripped.strip():  # Only comment non-empty lines
                    new_lines.append(f"{{{{/* {stripped} */}}}}\n")
                else:
                    new_lines.append(line)

        write_file_content(target_file, "".join(new_lines))

    def validate_full_template(self) -> HelmResult:
        """Run helm template on the original chart without modifications."""
        return self.executor.run_template(str(self.chart_path))


class BlockRangeExecutor:
    """Executes templates with specific block ranges for binary search."""

    def __init__(self, executor: HelmExecutor, chart_path: str):
        self.executor = executor
        self.chart_path = Path(chart_path)

    def execute_block_range(
        self,
        template: ParsedTemplate,
        start_block: int,
        end_block: int
    ) -> HelmResult:
        """Execute with blocks from start_block to end_block (inclusive)."""
        blocks_to_include = template.blocks[start_block:end_block + 1]

        with TempChartManager(str(self.chart_path)) as temp_manager:
            temp_chart = temp_manager.create_chart_copy()
            self._create_range_template(temp_chart, template, blocks_to_include)
            return self.executor.run_template(str(temp_chart))

    def _create_range_template(
        self,
        temp_chart: Path,
        template: ParsedTemplate,
        blocks_to_include: list[TemplateBlock]
    ):
        """Create template with only specified block range."""
        rel_path = template.file_path.relative_to(self.chart_path)
        target_file = temp_chart / rel_path

        included_lines = set()
        for block in blocks_to_include:
            for line_num in range(block.start_line, block.end_line + 1):
                included_lines.add(line_num)

        original_lines = template.original_content.splitlines(keepends=True)
        new_lines = []

        for i, line in enumerate(original_lines):
            line_num = i + 1
            if line_num in included_lines:
                new_lines.append(line)
            else:
                stripped = line.rstrip('\n\r')
                if stripped.strip():
                    new_lines.append(f"{{{{/* {stripped} */}}}}\n")
                else:
                    new_lines.append(line)

        write_file_content(target_file, "".join(new_lines))
