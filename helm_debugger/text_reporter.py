"""
Human-readable terminal output for the Helm template debugger.
"""

import os
import platform
import sys

from .engine import DebugReport, Finding, Listener
from .search import SearchMode, SearchStep
from .template_parser import ChartTemplates, ParsedTemplate


class Colors:
    """ANSI color codes for terminal output."""
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"

    @classmethod
    def disable(cls):
        """Disable colors (for non-TTY output)."""
        for name in ("RED", "GREEN", "YELLOW", "BLUE", "MAGENTA", "CYAN", "WHITE", "BOLD", "DIM", "RESET"):
            setattr(cls, name, "")


def configure_console(no_color: bool = False):
    """UTF-8 output everywhere; colors only on a capable terminal."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower() != "utf-8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    if no_color or os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        Colors.disable()
    elif platform.system() == "Windows":
        # Enable ANSI escape processing on Windows 10+
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            Colors.disable()


RULE = "-" * 40


class TextReporter(Listener):
    """Streams progress while searching, then prints the final report."""

    def __init__(self, verbose: bool = False, out=None):
        self.verbose = verbose
        self.out = out or sys.stdout

    def _print(self, text: str = ""):
        print(text, file=self.out)

    def print_header(self, title: str):
        self._print(f"\n{Colors.BOLD}{Colors.CYAN}{title}{Colors.RESET}")
        self._print(f"{Colors.DIM}{'-' * 50}{Colors.RESET}")

    # ── live progress ──────────────────────────────────────────────────

    def on_info(self, message: str):
        if self.verbose or message.startswith("Auto-targeting"):
            self._print(f"{Colors.CYAN}Info: {message}{Colors.RESET}")

    def on_warning(self, message: str):
        self._print(f"{Colors.YELLOW}Warning: {message}{Colors.RESET}")

    def on_chart(self, chart: ChartTemplates):
        self.print_header("Chart Analysis")
        self._print(f"  Chart path:     {chart.chart_path}")
        self._print(f"  Template files: {chart.total_files}")
        self._print(f"  Total blocks:   {chart.total_blocks}")
        if self.verbose:
            self._print(f"\n  {Colors.DIM}Files:{Colors.RESET}")
            for template in chart.templates:
                self._print(f"    - {template.display_name}: {len(template.blocks)} blocks")

    def on_search_start(self, template: ParsedTemplate, mode: SearchMode, strategy: str):
        mode_name = "Binary Search" if mode == SearchMode.BINARY else "Step-by-Step"
        kind = "YAML error" if strategy == "yaml" else "Go template error"
        self._print(f"\n{Colors.BOLD}[{mode_name} Mode]{Colors.RESET} "
                    f"{Colors.DIM}{kind} in {template.display_name}{Colors.RESET}")

    def on_step(self, step: SearchStep):
        status = f"{Colors.GREEN}OK{Colors.RESET}" if step.passed else f"{Colors.RED}FAIL{Colors.RESET}"
        self._print(f"  Testing lines {step.lines_tested}... {status}")

    # ── final report ───────────────────────────────────────────────────

    def finish(self, report: DebugReport) -> int:
        if report.status == "error":
            print(f"{Colors.RED}Error: {report.message}{Colors.RESET}", file=sys.stderr)
        elif report.status == "success":
            self._print_success(report)
        elif report.finding:
            self._print_finding(report.finding)
        else:
            self._print_unlocated(report)
        return report.exit_code

    def _print_success(self, report: DebugReport):
        if report.chart is not None and report.chart.total_files == 0:
            return
        self.print_header("Result")
        self._print(f"  {Colors.GREEN}OK Template renders successfully{Colors.RESET}")
        if self.verbose and report.rendered_output:
            self._print(f"\n{Colors.DIM}Rendered output preview (first 20 lines):{Colors.RESET}")
            for line in report.rendered_output.splitlines()[:20]:
                self._print(f"  {line}")

    def _print_unlocated(self, report: DebugReport):
        self.print_header("Result")
        self._print(f"  {Colors.RED}Helm failed to render the chart{Colors.RESET}")
        self._print_helm_error(report.helm_error)

    def _print_finding(self, finding: Finding):
        location = f"{finding.file}:{finding.line}" if finding.line else finding.file
        self._print(f"\n{Colors.BOLD}{Colors.RED}{'=' * 55}{Colors.RESET}")
        self._print(f"{Colors.BOLD}{Colors.RED}  ERROR FOUND: {location}{Colors.RESET}")
        self._print(f"{Colors.BOLD}{Colors.RED}{'=' * 55}{Colors.RESET}")

        self._print_code_context(finding)
        self._print_rendered_context(finding)
        self._print_helm_error(finding.helm_error)

        if finding.values:
            self._print(f"\n{Colors.BOLD}{Colors.YELLOW}Values Diagnosis:{Colors.RESET}")
            self._print(f"  {finding.values.message}")

        self._print(f"\n{Colors.BOLD}Details:{Colors.RESET}")
        self._print(f"  Category: {finding.category} (risk: {finding.risk})")
        if finding.line:
            span = f"{finding.line}-{finding.end_line}" if finding.end_line and finding.end_line != finding.line else str(finding.line)
            self._print(f"  Failing line: {span}")
        if finding.last_successful_line is not None:
            self._print(f"  Last successful line: {finding.last_successful_line}")
        if finding.located_by == "structure":
            self._print(f"  {Colors.DIM}Located by template structure analysis (unclosed block){Colors.RESET}")
        elif finding.located_by == "helm":
            self._print(f"  {Colors.DIM}Located from Helm's error message{Colors.RESET}")

        if finding.search_steps:
            self._print(f"\n{Colors.DIM}Search completed in {finding.search_steps} steps{Colors.RESET}")

        if finding.suggestions:
            self._print(f"\n{Colors.BOLD}Suggestions:{Colors.RESET}")
            for suggestion in finding.suggestions:
                self._print(f"  - {suggestion}")

    def _print_code_context(self, finding: Finding):
        if not finding.line:
            return
        before, after = finding.context()
        self._print(f"\n{Colors.DIM}{RULE}{Colors.RESET}")
        for n, content in before:
            self._print(f"   {Colors.DIM}{n:>4}{Colors.RESET} | {content}")
        for n, content in finding.failing_lines():
            self._print(f"{Colors.RED}>>{Colors.RESET} {Colors.RED}{n:>4}{Colors.RESET} | {Colors.RED}{content}{Colors.RESET}")
        for n, content in after:
            self._print(f"   {Colors.DIM}{n:>4}{Colors.RESET} | {content}")
        self._print(f"{Colors.DIM}{RULE}{Colors.RESET}")

    def _print_rendered_context(self, finding: Finding):
        if not finding.rendered_tail:
            return
        self._print(f"\n{Colors.BOLD}{Colors.MAGENTA}Rendered Manifest (before failure):{Colors.RESET}")
        self._print(f"{Colors.DIM}{RULE}{Colors.RESET}")
        for n, content in finding.rendered_tail:
            self._print(f"  {Colors.DIM}{n:>6}{Colors.RESET} | {content}")
        self._print(f"{Colors.DIM}{RULE}{Colors.RESET}")
        if finding.rendered_error_line:
            self._print(f"  {Colors.DIM}(Helm reports error at rendered line {finding.rendered_error_line}){Colors.RESET}")

    def _print_helm_error(self, message: str):
        if not message:
            return
        self._print(f"\n{Colors.BOLD}Helm Error:{Colors.RESET}")
        self._print(f"{Colors.DIM}{RULE}{Colors.RESET}")
        for line in message.strip().splitlines():
            self._print(f"  {Colors.RED}{line}{Colors.RESET}")
