"""
Output reporter for Helm template debugger.
Handles formatting and displaying results to the user.
"""

import re
import sys
import io
import platform
from dataclasses import dataclass
from typing import Optional

from parser import ParsedTemplate, TemplateBlock, ChartTemplates
from searcher import SearchResult, SearchStep, SearchMode

# Ensure UTF-8 encoding for stdout/stderr on Windows
if platform.system() == "Windows":
    if sys.stdout.encoding != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    if sys.stderr.encoding != 'utf-8':
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')


# ANSI color codes
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
        cls.RED = ""
        cls.GREEN = ""
        cls.YELLOW = ""
        cls.BLUE = ""
        cls.MAGENTA = ""
        cls.CYAN = ""
        cls.WHITE = ""
        cls.BOLD = ""
        cls.DIM = ""
        cls.RESET = ""


# Check if we're outputting to a terminal and handle Windows
if not sys.stdout.isatty():
    Colors.disable()
elif platform.system() == "Windows":
    # Enable ANSI color support on Windows 10+
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        # If ANSI support fails, disable colors
        Colors.disable()


class Reporter:
    """Handles all output formatting for the debugger."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def print_header(self, title: str):
        """Print a section header."""
        print(f"\n{Colors.BOLD}{Colors.CYAN}{title}{Colors.RESET}")
        print(f"{Colors.DIM}{'-' * 50}{Colors.RESET}")

    def print_chart_info(self, chart_templates: ChartTemplates):
        """Print information about the parsed chart."""
        self.print_header("Chart Analysis")
        print(f"  Chart path:     {chart_templates.chart_path}")
        print(f"  Template files: {chart_templates.total_files}")
        print(f"  Total blocks:   {chart_templates.total_blocks}")

        if self.verbose:
            print(f"\n  {Colors.DIM}Files:{Colors.RESET}")
            for template in chart_templates.templates:
                print(f"    - {template.file_path.name}: {len(template.blocks)} blocks")

    def print_search_mode(self, mode: SearchMode):
        """Print the search mode being used."""
        mode_name = "Binary Search" if mode == SearchMode.BINARY else "Step-by-Step"
        print(f"\n{Colors.BOLD}[{mode_name} Mode]{Colors.RESET}")

    def print_step_progress(self, step: SearchStep):
        """Print progress for a single search step."""
        if step.passed:
            status = f"{Colors.GREEN}OK{Colors.RESET}"
        else:
            status = f"{Colors.RED}FAIL{Colors.RESET}"

        print(f"  Testing blocks {step.blocks_tested}... {status}")

    def print_file_step_progress(self, file_name: str, step: SearchStep):
        """Print progress for a search step with file context."""
        if step.passed:
            status = f"{Colors.GREEN}OK{Colors.RESET}"
        else:
            status = f"{Colors.RED}X{Colors.RESET}"

        print(f"  [{file_name}] blocks {step.blocks_tested}: {status}")

    def print_search_result(self, result: SearchResult):
        """Print the search result."""
        if not result.found_error:
            self.print_header("Result")
            print(f"  {Colors.GREEN}OK No errors found in template{Colors.RESET}")
            return

        self._print_error_found(result)

    def _print_error_found(self, result: SearchResult):
        """Print detailed error information."""
        block = result.failing_block
        template = result.template

        # Error header
        print(f"\n{Colors.BOLD}{Colors.RED}{'=' * 55}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.RED}  ERROR FOUND: {template.file_path.name}:{block.start_line}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.RED}{'=' * 55}{Colors.RESET}")

        # Show context around the error
        self._print_code_context(template, block)

        # Show rendered manifest near failure
        self._print_rendered_context(result)

        # Show Helm error message
        if result.error_result and result.error_result.stderr:
            print(f"\n{Colors.BOLD}Helm Error:{Colors.RESET}")
            print(f"{Colors.DIM}{'-' * 40}{Colors.RESET}")
            error_msg = result.error_result.error_message or result.error_result.stderr
            for line in error_msg.strip().splitlines():
                print(f"  {Colors.RED}{line}{Colors.RESET}")

        # Show block info
        print(f"\n{Colors.BOLD}Block Details:{Colors.RESET}")
        print(f"  Type:  {block.block_type.value}")
        print(f"  Index: {result.failing_block_index}")
        if result.last_successful_block_index is not None:
            print(f"  Last successful block: {result.last_successful_block_index}")

        # Search statistics
        if result.steps:
            print(f"\n{Colors.DIM}Search completed in {len(result.steps)} steps{Colors.RESET}")

    def _print_code_context(
        self,
        template: ParsedTemplate,
        block: TemplateBlock,
        context_lines: int = 3
    ):
        """Print code context around the failing block."""
        lines = template.original_content.splitlines()
        total_lines = len(lines)

        start = max(0, block.start_line - context_lines - 1)
        end = min(total_lines, block.end_line + context_lines)

        print(f"\n{Colors.DIM}{'-' * 40}{Colors.RESET}")

        for i in range(start, end):
            line_num = i + 1
            line_content = lines[i] if i < len(lines) else ""

            # Determine if this is the error line
            is_error_line = block.start_line <= line_num <= block.end_line

            # Format line number
            line_num_str = f"{line_num:>4}"

            if is_error_line:
                prefix = f"{Colors.RED}>>{Colors.RESET}"
                line_num_fmt = f"{Colors.RED}{line_num_str}{Colors.RESET}"
                content_fmt = f"{Colors.RED}{line_content}{Colors.RESET}"
            else:
                prefix = "  "
                line_num_fmt = f"{Colors.DIM}{line_num_str}{Colors.RESET}"
                content_fmt = line_content

            print(f"{prefix} {line_num_fmt} | {content_fmt}")

        print(f"{Colors.DIM}{'-' * 40}{Colors.RESET}")

    def _extract_file_section(self, rendered_output: str, file_name: str) -> Optional[str]:
        """Extract a single file's rendered YAML from combined helm template output."""
        if not rendered_output:
            return None

        # helm template output separates files with:
        #   ---
        #   # Source: <chart>/templates/<file_name>
        sections = re.split(r'^---\s*$', rendered_output, flags=re.MULTILINE)

        for section in sections:
            # Check if this section belongs to the target file
            header_match = re.search(
                r'#\s*Source:\s*\S+/templates/' + re.escape(file_name),
                section
            )
            if header_match:
                # Return the section content after the Source header line
                lines = section.splitlines()
                content_lines = []
                past_header = False
                for line in lines:
                    if past_header:
                        content_lines.append(line)
                    elif re.match(r'#\s*Source:', line):
                        past_header = True
                    # skip blank lines before the header
                if content_lines:
                    return "\n".join(content_lines)
                # If no lines after header, return trimmed section
                return section.strip()

        return None

    def _print_rendered_context(self, result, context_lines: int = 5):
        """
        Print the rendered manifest near the failure point.
        Works with both SearchResult and LineSearchResult (duck-typed).
        """
        last_result = getattr(result, 'last_successful_result', None)
        if not last_result or not last_result.stdout:
            return

        template = getattr(result, 'template', None)
        if not template:
            return

        file_name = template.file_path.name
        section = self._extract_file_section(last_result.stdout, file_name)

        if not section:
            # Fall back to full stdout if we can't isolate the file
            section = last_result.stdout

        rendered_lines = section.splitlines()
        if not rendered_lines:
            return

        # Show the tail of the rendered section (the area right before the failure)
        total = len(rendered_lines)
        start = max(0, total - context_lines)
        display_lines = rendered_lines[start:]

        print(f"\n{Colors.BOLD}{Colors.MAGENTA}Rendered Manifest (before failure):{Colors.RESET}")
        print(f"{Colors.DIM}{'-' * 40}{Colors.RESET}")

        for i, line in enumerate(display_lines):
            line_num = start + i + 1
            line_num_str = f"{line_num:>6}"
            print(f"  {Colors.DIM}{line_num_str}{Colors.RESET} | {line}")

        print(f"{Colors.DIM}{'-' * 40}{Colors.RESET}")

        # If the error result has a rendered line number, mention it
        error_result = getattr(result, 'error_result', None)
        if error_result:
            rendered_error_line = error_result.get_failing_line()
            if rendered_error_line:
                print(f"  {Colors.DIM}(Helm reports error at rendered line {rendered_error_line}){Colors.RESET}")

    def print_multi_file_results(self, results: dict[str, SearchResult]):
        """Print results from multiple file search."""
        self.print_header("Multi-File Search Results")

        errors_found = []
        for file_name, result in results.items():
            if result.found_error:
                errors_found.append((file_name, result))
                status = f"{Colors.RED}X Error at line {result.failing_block.start_line}{Colors.RESET}"
            else:
                status = f"{Colors.GREEN}OK OK{Colors.RESET}"

            print(f"  {file_name}: {status}")

        # Print detailed errors
        for file_name, result in errors_found:
            print(f"\n{Colors.BOLD}Details for {file_name}:{Colors.RESET}")
            self._print_error_found(result)

    def print_no_helm_error(self, result):
        """Print message when Helm doesn't produce an error."""
        self.print_header("Result")
        print(f"  {Colors.GREEN}OK Template renders successfully{Colors.RESET}")
        if self.verbose and result.stdout:
            print(f"\n{Colors.DIM}Rendered output preview (first 20 lines):{Colors.RESET}")
            for line in result.stdout.splitlines()[:20]:
                print(f"  {line}")

    def print_error(self, message: str):
        """Print an error message."""
        print(f"{Colors.RED}Error: {message}{Colors.RESET}", file=sys.stderr)

    def print_warning(self, message: str):
        """Print a warning message."""
        print(f"{Colors.YELLOW}Warning: {message}{Colors.RESET}")

    def print_info(self, message: str):
        """Print an info message."""
        print(f"{Colors.CYAN}Info: {message}{Colors.RESET}")

    def print_suggestions(self, result: SearchResult):
        """Print suggestions for fixing common errors."""
        if not result.error_result:
            return

        error_msg = result.error_result.stderr.lower()
        suggestions = []

        # Common error patterns and suggestions
        if "unexpected" in error_msg and "}" in error_msg:
            suggestions.append("Check for mismatched or missing braces {{ }}")

        if "undefined" in error_msg:
            suggestions.append("Verify the variable exists in values.yaml")
            suggestions.append("Check for typos in variable names")

        if "not defined" in error_msg:
            suggestions.append("The referenced template or helper may not exist")

        if "cannot range over" in error_msg:
            suggestions.append("Ensure the value is a list or map before ranging")

        if "nil pointer" in error_msg:
            suggestions.append("Add a nil check: {{- if .Values.something }}")

        if suggestions:
            print(f"\n{Colors.BOLD}Suggestions:{Colors.RESET}")
            for suggestion in suggestions:
                print(f"  - {suggestion}")

    # Line-based search methods

    def print_line_step_progress(self, step):
        """Print progress for a line-based search step."""
        if step.passed:
            status = f"{Colors.GREEN}OK{Colors.RESET}"
        else:
            status = f"{Colors.RED}FAIL{Colors.RESET}"

        print(f"  Testing lines {step.lines_tested}... {status}")

    def print_line_search_result(self, result):
        """Print line-based search result."""
        if not result.found_error:
            self.print_header("Result")
            print(f"  {Colors.GREEN}OK No errors found in template{Colors.RESET}")
            return

        self._print_line_error_found(result)

    def _print_line_error_found(self, result):
        """Print detailed error information for line-based search."""
        template = result.template

        # Error header
        print(f"\n{Colors.BOLD}{Colors.RED}{'=' * 55}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.RED}  ERROR FOUND: {template.file_path.name}:{result.failing_line}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.RED}{'=' * 55}{Colors.RESET}")

        # Show context
        self._print_line_context(result)

        # Show rendered manifest near failure
        self._print_rendered_context(result)

        # Show Helm error message
        if result.error_result and result.error_result.stderr:
            print(f"\n{Colors.BOLD}Helm Error:{Colors.RESET}")
            print(f"{Colors.DIM}{'-' * 40}{Colors.RESET}")
            error_msg = result.error_result.error_message or result.error_result.stderr
            for line in error_msg.strip().splitlines():
                print(f"  {Colors.RED}{line}{Colors.RESET}")

        # Show line info
        print(f"\n{Colors.BOLD}Details:{Colors.RESET}")
        print(f"  Failing line: {result.failing_line}")
        if result.last_successful_line is not None:
            print(f"  Last successful line: {result.last_successful_line}")

        # Search statistics
        if result.steps:
            print(f"\n{Colors.DIM}Search completed in {len(result.steps)} steps{Colors.RESET}")

    def _print_line_context(self, result):
        """Print code context for line-based search result."""
        print(f"\n{Colors.DIM}{'-' * 40}{Colors.RESET}")

        # Context before
        start_line = result.failing_line - len(result.context_before)
        for i, content in enumerate(result.context_before):
            line_num = start_line + i
            line_num_str = f"{line_num:>4}"
            print(f"   {Colors.DIM}{line_num_str}{Colors.RESET} | {content}")

        # Failing line
        line_num_str = f"{result.failing_line:>4}"
        print(f"{Colors.RED}>>{Colors.RESET} {Colors.RED}{line_num_str}{Colors.RESET} | {Colors.RED}{result.failing_content}{Colors.RESET}")

        # Context after
        for i, content in enumerate(result.context_after):
            line_num = result.failing_line + 1 + i
            line_num_str = f"{line_num:>4}"
            print(f"   {Colors.DIM}{line_num_str}{Colors.RESET} | {content}")

        print(f"{Colors.DIM}{'-' * 40}{Colors.RESET}")

    def print_line_suggestions(self, result):
        """Print suggestions for line-based search results."""
        if not result.error_result:
            return

        error_msg = result.error_result.stderr.lower()
        suggestions = []

        # YAML-specific error patterns
        if "yaml" in error_msg:
            if "indentation" in error_msg or "indent" in error_msg:
                suggestions.append("Check indentation - YAML requires consistent spacing")
            if "did not find expected" in error_msg:
                suggestions.append("Check YAML structure - possibly wrong indentation or missing item")
            if "mapping" in error_msg:
                suggestions.append("Check YAML key-value syntax (key: value)")

        # Common error patterns
        if "unexpected" in error_msg and "}" in error_msg:
            suggestions.append("Check for mismatched or missing braces {{ }}")

        if "undefined" in error_msg:
            suggestions.append("Verify the variable exists in values.yaml")

        if suggestions:
            print(f"\n{Colors.BOLD}Suggestions:{Colors.RESET}")
            for suggestion in suggestions:
                print(f"  - {suggestion}")
