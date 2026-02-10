"""
Line-based search algorithm for finding YAML errors in Helm templates.
Preserves template control structures while bisecting content.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable

from parser import ParsedTemplate, BlockType
from executor import HelmExecutor, HelmResult
from utils import TempChartManager, write_file_content


@dataclass
class LineSearchStep:
    """Represents a step in line-based search."""
    step_number: int
    lines_tested: str  # e.g., "1-50"
    result: HelmResult
    line_number: Optional[int] = None

    @property
    def passed(self) -> bool:
        return self.result.success


@dataclass
class LineSearchResult:
    """Result of line-based search."""
    found_error: bool
    failing_line: Optional[int] = None
    failing_content: Optional[str] = None
    last_successful_line: Optional[int] = None
    steps: list[LineSearchStep] = None
    error_result: Optional[HelmResult] = None
    last_successful_result: Optional[HelmResult] = None
    template: Optional[ParsedTemplate] = None
    context_before: list[str] = None
    context_after: list[str] = None

    def __post_init__(self):
        if self.steps is None:
            self.steps = []
        if self.context_before is None:
            self.context_before = []
        if self.context_after is None:
            self.context_after = []


class TemplateStructureAnalyzer:
    """Analyzes template structure to identify control flow lines."""

    # Patterns for template control structures
    CONTROL_PATTERNS = [
        re.compile(r'\{\{-?\s*if\s'),
        re.compile(r'\{\{-?\s*else\s*-?\}\}'),
        re.compile(r'\{\{-?\s*else\s+if\s'),
        re.compile(r'\{\{-?\s*end\s*-?\}\}'),
        re.compile(r'\{\{-?\s*range\s'),
        re.compile(r'\{\{-?\s*with\s'),
        re.compile(r'\{\{-?\s*define\s'),
        re.compile(r'\{\{-?\s*template\s'),
        re.compile(r'\{\{-?\s*block\s'),
    ]

    def is_control_line(self, line: str) -> bool:
        """Check if a line contains a template control structure."""
        for pattern in self.CONTROL_PATTERNS:
            if pattern.search(line):
                return True
        return False

    def get_structure_map(self, content: str) -> dict[int, bool]:
        """
        Returns a map of line_number -> is_control_structure.
        Control structure lines should NOT be commented out.
        """
        lines = content.splitlines()
        structure_map = {}

        for i, line in enumerate(lines):
            line_num = i + 1
            structure_map[line_num] = self.is_control_line(line)

        return structure_map


class LineBasedExecutor:
    """Executes templates with line-based modifications."""

    def __init__(self, executor: HelmExecutor, chart_path: str):
        self.executor = executor
        self.chart_path = Path(chart_path)
        self.analyzer = TemplateStructureAnalyzer()

    def execute_up_to_line(
        self,
        template: ParsedTemplate,
        end_line: int
    ) -> HelmResult:
        """
        Execute template including content up to end_line.
        Lines after end_line are commented out, but control structures preserved.
        """
        with TempChartManager(str(self.chart_path)) as temp_manager:
            temp_chart = temp_manager.create_chart_copy()
            self._create_partial_by_line(temp_chart, template, end_line)
            return self.executor.run_template(str(temp_chart))

    def _create_partial_by_line(
        self,
        temp_chart: Path,
        template: ParsedTemplate,
        end_line: int
    ):
        """Create template with lines after end_line commented out."""
        rel_path = template.file_path.relative_to(self.chart_path)
        target_file = temp_chart / rel_path

        lines = template.original_content.splitlines(keepends=True)
        structure_map = self.analyzer.get_structure_map(template.original_content)

        new_lines = []
        for i, line in enumerate(lines):
            line_num = i + 1

            if line_num <= end_line:
                # Include this line as-is
                new_lines.append(line)
            elif structure_map.get(line_num, False):
                # Keep control structure lines intact
                new_lines.append(line)
            else:
                # Comment out content lines
                stripped = line.rstrip('\n\r')
                if stripped.strip():
                    # Replace with empty line to preserve structure
                    new_lines.append("\n")
                else:
                    new_lines.append(line)

        write_file_content(target_file, "".join(new_lines))


class LineBinarySearcher:
    """Binary search on source lines to find YAML errors."""

    def __init__(
        self,
        executor: HelmExecutor,
        chart_path: str,
        progress_callback: Optional[Callable[[LineSearchStep], None]] = None
    ):
        self.executor = executor
        self.chart_path = chart_path
        self.line_executor = LineBasedExecutor(executor, chart_path)
        self.progress_callback = progress_callback

    def search(self, template: ParsedTemplate) -> LineSearchResult:
        """Perform binary search on lines to find the failing line."""
        total_lines = template.total_lines
        if total_lines == 0:
            return LineSearchResult(found_error=False, template=template)

        steps = []
        step_number = 0

        low = 1
        high = total_lines
        last_successful = 0
        failing_line = None
        last_successful_result = None

        while low <= high:
            mid = (low + high) // 2
            step_number += 1

            result = self.line_executor.execute_up_to_line(template, mid)

            step = LineSearchStep(
                step_number=step_number,
                lines_tested=f"1-{mid}",
                result=result,
                line_number=mid
            )
            steps.append(step)

            if self.progress_callback:
                self.progress_callback(step)

            if result.success:
                last_successful = mid
                last_successful_result = result
                low = mid + 1
            else:
                failing_line = mid
                high = mid - 1

        if failing_line is not None:
            lines = template.original_content.splitlines()
            failing_content = lines[failing_line - 1] if failing_line <= len(lines) else ""

            # Get context
            context_before = []
            context_after = []
            for i in range(max(0, failing_line - 4), failing_line - 1):
                if i < len(lines):
                    context_before.append(lines[i])
            for i in range(failing_line, min(len(lines), failing_line + 3)):
                context_after.append(lines[i])

            # Get final error
            error_result = self.line_executor.execute_up_to_line(template, failing_line)

            return LineSearchResult(
                found_error=True,
                failing_line=failing_line,
                failing_content=failing_content,
                last_successful_line=last_successful if last_successful > 0 else None,
                steps=steps,
                error_result=error_result,
                last_successful_result=last_successful_result,
                template=template,
                context_before=context_before,
                context_after=context_after
            )

        return LineSearchResult(
            found_error=False,
            template=template,
            steps=steps
        )


class LineStepByStepSearcher:
    """Step-by-step line search for detailed debugging."""

    def __init__(
        self,
        executor: HelmExecutor,
        chart_path: str,
        progress_callback: Optional[Callable[[LineSearchStep], None]] = None
    ):
        self.executor = executor
        self.chart_path = chart_path
        self.line_executor = LineBasedExecutor(executor, chart_path)
        self.progress_callback = progress_callback

    def search(
        self,
        template: ParsedTemplate,
        start_line: int = 1
    ) -> LineSearchResult:
        """Execute template line by line until error is found."""
        total_lines = template.total_lines
        if total_lines == 0:
            return LineSearchResult(found_error=False, template=template)

        steps = []
        last_successful = 0
        last_successful_result = None

        for line_num in range(start_line, total_lines + 1):
            result = self.line_executor.execute_up_to_line(template, line_num)

            step = LineSearchStep(
                step_number=line_num - start_line + 1,
                lines_tested=f"1-{line_num}",
                result=result,
                line_number=line_num
            )
            steps.append(step)

            if self.progress_callback:
                self.progress_callback(step)

            if result.success:
                last_successful = line_num
                last_successful_result = result
            else:
                lines = template.original_content.splitlines()
                failing_content = lines[line_num - 1] if line_num <= len(lines) else ""

                context_before = []
                context_after = []
                for i in range(max(0, line_num - 4), line_num - 1):
                    if i < len(lines):
                        context_before.append(lines[i])
                for i in range(line_num, min(len(lines), line_num + 3)):
                    context_after.append(lines[i])

                return LineSearchResult(
                    found_error=True,
                    failing_line=line_num,
                    failing_content=failing_content,
                    last_successful_line=last_successful if last_successful > 0 else None,
                    steps=steps,
                    error_result=result,
                    last_successful_result=last_successful_result,
                    template=template,
                    context_before=context_before,
                    context_after=context_after
                )

        return LineSearchResult(
            found_error=False,
            last_successful_line=total_lines,
            template=template,
            steps=steps
        )
