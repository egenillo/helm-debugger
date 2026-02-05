"""
Search algorithms for finding template errors.
Implements binary search and step-by-step debugging modes.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable

from parser import ParsedTemplate, TemplateBlock, ChartTemplates
from executor import HelmExecutor, IncrementalExecutor, HelmResult


class SearchMode(Enum):
    """Available search modes."""
    BINARY = "binary"
    STEP_BY_STEP = "step"


@dataclass
class SearchStep:
    """Represents a single step in the search process."""
    step_number: int
    blocks_tested: str  # e.g., "0-15" or "0-7"
    result: HelmResult
    block_index: Optional[int] = None

    @property
    def passed(self) -> bool:
        return self.result.success


@dataclass
class SearchResult:
    """Result of the search process."""
    found_error: bool
    failing_block: Optional[TemplateBlock] = None
    failing_block_index: Optional[int] = None
    last_successful_block_index: Optional[int] = None
    steps: list[SearchStep] = None
    error_result: Optional[HelmResult] = None
    template: Optional[ParsedTemplate] = None

    def __post_init__(self):
        if self.steps is None:
            self.steps = []


class BinarySearcher:
    """Binary search algorithm for finding template errors."""

    def __init__(
        self,
        executor: HelmExecutor,
        chart_path: str,
        progress_callback: Optional[Callable[[SearchStep], None]] = None
    ):
        self.executor = executor
        self.chart_path = chart_path
        self.progress_callback = progress_callback
        self.inc_executor = IncrementalExecutor(executor, chart_path)

    def search(self, template: ParsedTemplate) -> SearchResult:
        """
        Perform binary search to find the failing block.
        Returns the first block that causes failure.
        """
        blocks = template.blocks
        if not blocks:
            return SearchResult(found_error=False, template=template)

        total_blocks = len(blocks)
        steps = []
        step_number = 0

        # First, verify the full template fails
        full_result = self.inc_executor.validate_full_template()
        if full_result.success:
            return SearchResult(
                found_error=False,
                template=template,
                steps=steps
            )

        low = 0
        high = total_blocks - 1
        last_successful = -1
        failing_index = None

        while low <= high:
            mid = (low + high) // 2
            step_number += 1

            # Test blocks 0 to mid
            result = self.inc_executor.execute_up_to_block(template, mid)

            step = SearchStep(
                step_number=step_number,
                blocks_tested=f"0-{mid}",
                result=result,
                block_index=mid
            )
            steps.append(step)

            if self.progress_callback:
                self.progress_callback(step)

            if result.success:
                # Error is after mid
                last_successful = mid
                low = mid + 1
            else:
                # Error is at or before mid
                failing_index = mid
                high = mid - 1

        # Determine the exact failing block
        if failing_index is not None:
            failing_block = blocks[failing_index]
            error_result = self.inc_executor.execute_up_to_block(template, failing_index)

            return SearchResult(
                found_error=True,
                failing_block=failing_block,
                failing_block_index=failing_index,
                last_successful_block_index=last_successful if last_successful >= 0 else None,
                steps=steps,
                error_result=error_result,
                template=template
            )

        return SearchResult(
            found_error=False,
            template=template,
            steps=steps
        )


class StepByStepSearcher:
    """Step-by-step search for detailed debugging."""

    def __init__(
        self,
        executor: HelmExecutor,
        chart_path: str,
        progress_callback: Optional[Callable[[SearchStep], None]] = None
    ):
        self.executor = executor
        self.chart_path = chart_path
        self.progress_callback = progress_callback
        self.inc_executor = IncrementalExecutor(executor, chart_path)

    def search(
        self,
        template: ParsedTemplate,
        start_from: int = 0
    ) -> SearchResult:
        """
        Execute template block by block until an error is found.
        Returns detailed information about each step.
        """
        blocks = template.blocks
        if not blocks:
            return SearchResult(found_error=False, template=template)

        steps = []
        last_successful = -1

        for i in range(start_from, len(blocks)):
            result = self.inc_executor.execute_up_to_block(template, i)

            step = SearchStep(
                step_number=i - start_from + 1,
                blocks_tested=f"0-{i}",
                result=result,
                block_index=i
            )
            steps.append(step)

            if self.progress_callback:
                self.progress_callback(step)

            if result.success:
                last_successful = i
            else:
                # Found the failing block
                return SearchResult(
                    found_error=True,
                    failing_block=blocks[i],
                    failing_block_index=i,
                    last_successful_block_index=last_successful if last_successful >= 0 else None,
                    steps=steps,
                    error_result=result,
                    template=template
                )

        # No error found
        return SearchResult(
            found_error=False,
            last_successful_block_index=len(blocks) - 1,
            template=template,
            steps=steps
        )


class MultiFileSearcher:
    """Search across multiple template files."""

    def __init__(
        self,
        executor: HelmExecutor,
        chart_path: str,
        mode: SearchMode = SearchMode.BINARY,
        progress_callback: Optional[Callable[[str, SearchStep], None]] = None
    ):
        self.executor = executor
        self.chart_path = chart_path
        self.mode = mode
        self.progress_callback = progress_callback

    def search(self, chart_templates: ChartTemplates) -> dict[str, SearchResult]:
        """Search for errors in all template files."""
        results = {}

        for template in chart_templates.templates:
            file_name = template.file_path.name

            # Create file-specific progress callback
            file_callback = None
            if self.progress_callback:
                file_callback = lambda step, fn=file_name: self.progress_callback(fn, step)

            if self.mode == SearchMode.BINARY:
                searcher = BinarySearcher(
                    self.executor,
                    self.chart_path,
                    file_callback
                )
            else:
                searcher = StepByStepSearcher(
                    self.executor,
                    self.chart_path,
                    file_callback
                )

            results[file_name] = searcher.search(template)

        return results


def find_error(
    executor: HelmExecutor,
    chart_path: str,
    template: ParsedTemplate,
    mode: SearchMode = SearchMode.BINARY,
    progress_callback: Optional[Callable[[SearchStep], None]] = None
) -> SearchResult:
    """
    Convenience function to find an error in a template.
    """
    if mode == SearchMode.BINARY:
        searcher = BinarySearcher(executor, chart_path, progress_callback)
    else:
        searcher = StepByStepSearcher(executor, chart_path, progress_callback)

    return searcher.search(template)
