"""
Debugging engine: runs one complete debugging session and returns a DebugReport.
Output formats (text, JSON, SARIF) only render the report - they hold no logic.
"""

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import diagnostics
from .helm import ChartWorkspace, HelmResult, HelmRunner
from .search import PrefixSearcher, SearchMode, SearchResult, SearchStep
from .template_parser import ChartTemplates, ParsedTemplate, parse_chart
from .utils import find_helm_executable, validate_chart_directory
from .values import ValuesDiagnostic, diagnose as diagnose_values, yaml_available

EXIT_OK = 0
EXIT_ERROR_FOUND = 1
EXIT_TOOL_ERROR = 2


@dataclass
class Options:
    """Everything a debugging session needs, independent of the CLI."""
    chart: str
    values_files: list[str] = field(default_factory=list)
    set_values: list[str] = field(default_factory=list)
    mode: SearchMode = SearchMode.BINARY
    target_file: Optional[str] = None
    release_name: str = "debug-release"
    helm_path: Optional[str] = None
    kube_version: Optional[str] = None
    api_versions: list[str] = field(default_factory=list)
    dependency_update: bool = False
    helm_args: list[str] = field(default_factory=list)
    timeout: int = 60

    def extra_helm_args(self) -> list[str]:
        args = []
        if self.kube_version:
            args += ["--kube-version", self.kube_version]
        for api in self.api_versions:
            args += ["--api-versions", api]
        return args + list(self.helm_args)


@dataclass
class Finding:
    """The located cause of a rendering failure."""
    template: ParsedTemplate
    line: Optional[int]
    end_line: Optional[int]
    helm_error: str
    category: str
    risk: str
    suggestions: list[str]
    located_by: str                 # "search" | "structure" | "helm"
    last_successful_line: Optional[int] = None
    rendered_tail: list[tuple[int, str]] = field(default_factory=list)
    rendered_error_line: Optional[int] = None
    values: Optional[ValuesDiagnostic] = None
    search_steps: int = 0

    @property
    def file(self) -> str:
        return self.template.display_name

    @property
    def block_type(self) -> Optional[str]:
        block = self.template.block_at_line(self.line) if self.line else None
        return block.block_type.value if block else None

    def failing_lines(self) -> list[tuple[int, str]]:
        if not self.line:
            return []
        lines = self.template.lines
        end = self.end_line or self.line
        return [(n, lines[n - 1]) for n in range(self.line, end + 1) if n - 1 < len(lines)]

    def context(self, before: int = 3, after: int = 3) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
        if not self.line:
            return [], []
        lines = self.template.lines
        end = self.end_line or self.line
        ctx_before = [(n, lines[n - 1]) for n in range(max(1, self.line - before), self.line)]
        ctx_after = [(n, lines[n - 1]) for n in range(end + 1, min(len(lines), end + after) + 1)]
        return ctx_before, ctx_after


@dataclass
class DebugReport:
    """Outcome of a debugging session."""
    chart_path: str
    status: str = "success"          # "success" | "failure" | "error"
    message: str = ""                # tool error message (status == "error")
    chart: Optional[ChartTemplates] = None
    mode: Optional[SearchMode] = None
    strategy: Optional[str] = None   # "yaml" | "template"
    helm_error: str = ""             # Helm's error for the full chart
    finding: Optional[Finding] = None
    steps: list[SearchStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    helm_renders: int = 0
    rendered_output: str = ""        # full output when the chart renders fine

    @property
    def exit_code(self) -> int:
        return {"success": EXIT_OK, "failure": EXIT_ERROR_FOUND}.get(self.status, EXIT_TOOL_ERROR)

    @property
    def category(self) -> str:
        return self.finding.category if self.finding else diagnostics.classify(self.helm_error)


class Listener:
    """Progress hooks. Reporters override what they want to show live."""

    def on_info(self, message: str): ...
    def on_warning(self, message: str): ...
    def on_chart(self, chart: ChartTemplates): ...
    def on_search_start(self, template: ParsedTemplate, mode: SearchMode, strategy: str): ...
    def on_step(self, step: SearchStep): ...


def run(options: Options, listener: Optional[Listener] = None) -> DebugReport:
    """Run one debugging session."""
    listener = listener or Listener()
    report = DebugReport(chart_path=options.chart)

    def info(msg: str):
        report.info.append(msg)
        listener.on_info(msg)

    def warn(msg: str):
        report.warnings.append(msg)
        listener.on_warning(msg)

    def fail(msg: str) -> DebugReport:
        report.status = "error"
        report.message = msg
        return report

    is_valid, error_msg = validate_chart_directory(options.chart)
    if not is_valid:
        return fail(error_msg)

    helm_path = options.helm_path or find_helm_executable()
    if not helm_path:
        return fail("Helm executable not found. Install Helm or use --helm-path.")
    if not Path(helm_path).exists() and not shutil.which(helm_path):
        return fail(f"Helm executable not found: {helm_path}")
    info(f"Using Helm at: {helm_path}")

    try:
        chart = parse_chart(options.chart)
    except (OSError, ValueError) as e:
        return fail(f"Failed to parse chart: {e}")
    report.chart = chart
    listener.on_chart(chart)

    if chart.total_files == 0:
        warn("No template files found in chart.")
        return report

    runner = HelmRunner(
        helm_path=helm_path,
        release_name=options.release_name,
        values_files=options.values_files,
        set_values=options.set_values,
        extra_args=options.extra_helm_args(),
        timeout=options.timeout,
    )

    try:
        with ChartWorkspace(options.chart, runner, options.dependency_update) as workspace:
            _debug(options, report, chart, workspace, runner, listener, info, warn)
            report.helm_renders = workspace.render_count
    except RuntimeError as e:
        return fail(str(e))

    return report


def _debug(options, report, chart, workspace, runner, listener, info, warn):
    full = workspace.render()
    if full.success:
        report.rendered_output = full.stdout
        return

    report.status = "failure"
    report.helm_error = full.error_message
    info(f"Initial error: {full.error_message}")

    failing_rel = full.get_failing_rel_path()
    if failing_rel:
        line = full.get_source_line()
        info(f"Helm reports error in: {failing_rel}" + (f" (line {line})" if line else ""))

    targets = _select_targets(options, chart, failing_rel, info, warn)
    if targets is None:
        report.status = "error"
        report.message = f"Template file not found: {options.target_file}"
        return

    strategy = "yaml" if diagnostics.is_yaml_error(full.stderr) else "template"
    report.strategy = strategy
    report.mode = options.mode

    for template in targets:
        finding = _locate(options, template, full, workspace, runner, listener, report, strategy)
        if finding:
            report.finding = finding
            return

    if not targets:
        warn("Helm's error does not point to a template file (check Chart.yaml and values files).")
    else:
        warn("Could not pinpoint the failing line; showing Helm's error only.")


def _select_targets(options, chart: ChartTemplates, failing_rel, info, warn) -> Optional[list[ParsedTemplate]]:
    """Choose which templates to search, most likely first."""
    if options.target_file:
        found = [t for t in chart.templates if t.matches(options.target_file)]
        return found or None

    if failing_rel:
        template = chart.find(failing_rel)
        if template:
            info(f"Auto-targeting file: {template.display_name}")
            return [template]
        warn(f"Could not find template: {failing_rel}")

    info("Skipping helper files (_*.tpl) from debugging")
    return [t for t in chart.templates if not t.is_helper]


def _locate(options, template, full: HelmResult, workspace, runner, listener, report, strategy) -> Optional[Finding]:
    """Locate the failing line in one template, choosing the cheapest reliable strategy."""
    helm_line = full.get_source_line() if template.matches(full.get_failing_rel_path() or "") else None

    # 1. Unclosed {{ if }} / {{ range }} ...: the parser knows exactly which opener lacks its end.
    unclosed = template.structure.unclosed_openers
    if unclosed and "unexpected eof" in full.stderr.lower():
        line = unclosed[-1]
        return _finding(options, template, line, line, full, "structure", runner)

    # 2. Helper files only hold definitions: truncating them breaks every include,
    #    so trust the line Helm reports instead of searching.
    if template.is_helper:
        if helm_line:
            return _finding(options, template, helm_line, helm_line, full, "helm", runner)
        return None

    # 3. Search growing prefixes of the template.
    listener.on_search_start(template, options.mode, strategy)
    searcher = PrefixSearcher(workspace, options.mode, listener.on_step)
    result = searcher.search(template)
    report.steps.extend(result.steps)

    if result.found_error:
        return _finding_from_search(options, result, runner)

    # The template renders on its own but Helm still points at a line in it.
    if helm_line:
        return _finding(options, template, helm_line, helm_line, full, "helm", runner)
    return None


def _finding(options, template, line, end_line, helm_result: HelmResult, located_by, runner) -> Finding:
    error = helm_result.error_message
    category = diagnostics.classify(error)
    return Finding(
        template=template,
        line=line,
        end_line=end_line,
        helm_error=error,
        category=category,
        risk=diagnostics.risk_level(category),
        suggestions=diagnostics.suggestions_for(helm_result.stderr),
        located_by=located_by,
        values=_values_diagnostic(options, template, helm_result, runner),
    )


def _finding_from_search(options, result: SearchResult, runner) -> Finding:
    finding = _finding(
        options, result.template, result.failing_line, result.end_line,
        result.error_result, "search", runner,
    )
    finding.last_successful_line = result.last_successful_line
    finding.search_steps = len(result.steps)
    if result.last_successful_result:
        finding.rendered_tail = diagnostics.rendered_tail(
            result.last_successful_result.stdout, result.template.rel_path
        )
    if finding.category.startswith("yaml"):
        finding.rendered_error_line = result.error_result.get_rendered_line()
    return finding


def _values_diagnostic(options, template, helm_result, runner) -> Optional[ValuesDiagnostic]:
    if not yaml_available():
        return None
    return diagnose_values(
        helm_result.stderr,
        Path(options.chart),
        template.rel_path,
        runner.values_files,
        options.set_values,
    )
