"""
Search algorithms for finding the template line that breaks rendering.

The searcher renders growing prefixes of a template (lines 1..N, rest blanked,
open scopes auto-closed) and finds the smallest N that fails. It works for both
YAML errors and Go template errors, in binary (O(log n)) or step-by-step mode.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from .helm import ChartWorkspace, HelmResult
from .template_parser import ParsedTemplate


class SearchMode(Enum):
    """Available search modes."""
    BINARY = "binary"
    STEP_BY_STEP = "step"


@dataclass
class SearchStep:
    """Represents a single step in the search process."""
    step_number: int
    lines_tested: str  # e.g. "1-50"
    result: HelmResult
    line_number: int

    @property
    def passed(self) -> bool:
        return self.result.success


@dataclass
class SearchResult:
    """Result of searching one template."""
    found_error: bool
    template: ParsedTemplate
    failing_line: Optional[int] = None   # first line of the failing chunk
    end_line: Optional[int] = None       # last line of the failing chunk (multi-line actions)
    last_successful_line: Optional[int] = None
    steps: list[SearchStep] = field(default_factory=list)
    error_result: Optional[HelmResult] = None
    last_successful_result: Optional[HelmResult] = None


class PrefixSearcher:
    """Finds the first line whose inclusion makes `helm template` fail."""

    def __init__(
        self,
        workspace: ChartWorkspace,
        mode: SearchMode = SearchMode.BINARY,
        progress_callback: Optional[Callable[[SearchStep], None]] = None,
    ):
        self.workspace = workspace
        self.mode = mode
        self.progress_callback = progress_callback

    def search(self, template: ParsedTemplate) -> SearchResult:
        cuts = template.structure.cut_points
        if not cuts:
            return SearchResult(found_error=False, template=template)

        steps: list[SearchStep] = []
        results: dict[int, HelmResult] = {}

        def test(index: int) -> bool:
            line = cuts[index]
            result = self.workspace.render(template.rel_path, template.build_prefix(line))
            results[index] = result
            step = SearchStep(len(steps) + 1, f"1-{line}", result, line)
            steps.append(step)
            if self.progress_callback:
                self.progress_callback(step)
            return result.success

        if self.mode == SearchMode.BINARY:
            failing_index = self._binary(test, len(cuts))
        else:
            failing_index = self._linear(test, len(cuts))

        if failing_index is None:
            return SearchResult(
                found_error=False,
                template=template,
                last_successful_line=cuts[-1],
                steps=steps,
            )

        passed = [i for i, r in results.items() if r.success and i < failing_index]
        last_ok = max(passed) if passed else None
        start, end = self._failing_span(template, cuts, failing_index)

        return SearchResult(
            found_error=True,
            template=template,
            failing_line=start,
            end_line=end,
            last_successful_line=cuts[last_ok] if last_ok is not None else None,
            steps=steps,
            error_result=results[failing_index],
            last_successful_result=results[last_ok] if last_ok is not None else None,
        )

    @staticmethod
    def _binary(test: Callable[[int], bool], count: int) -> Optional[int]:
        low, high = 0, count - 1
        failing = None
        while low <= high:
            mid = (low + high) // 2
            if test(mid):
                low = mid + 1
            else:
                failing = mid
                high = mid - 1
        return failing

    @staticmethod
    def _linear(test: Callable[[int], bool], count: int) -> Optional[int]:
        for index in range(count):
            if not test(index):
                return index
        return None

    @staticmethod
    def _failing_span(template: ParsedTemplate, cuts: list[int], index: int) -> tuple[int, int]:
        """
        The failing chunk runs from the previous cut point + 1 to this cut point.
        It is more than one line when a {{ ... }} action spans several lines.
        Leading blank lines are skipped so the report points at real content.
        """
        end = cuts[index]
        start = cuts[index - 1] + 1 if index > 0 else 1
        lines = template.lines
        while start < end and not lines[start - 1].strip():
            start += 1
        return start, end
