"""
JSON reporter for Helm template debugger.
Produces structured, machine-readable output for CI/CD pipeline integration.
"""

import json
import sys
from typing import Optional

from parser import ParsedTemplate, TemplateBlock, ChartTemplates
from searcher import SearchResult, SearchStep, SearchMode


class JsonReporter:
    """
    Collects all debug events and produces a single JSON object on flush().
    Drop-in replacement for Reporter when -o json is used.
    """

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._data = {
            "version": "1.0",
            "status": "success",
            "exit_code": 0,
            "summary": {},
            "search": {},
            "error": None,
            "suggestions": [],
            "steps": [],
        }

    # ── helpers ────────────────────────────────────────────────────────

    def _risk_level(self, error_message: str) -> str:
        """Classify risk level based on error content."""
        if not error_message:
            return "none"
        low = error_message.lower()
        if any(k in low for k in ["nil pointer", "undefined", "not defined"]):
            return "high"
        if any(k in low for k in ["yaml", "indentation", "mapping"]):
            return "medium"
        return "low"

    def _error_category(self, error_message: str) -> str:
        """Classify error into a machine-readable category."""
        if not error_message:
            return "unknown"
        low = error_message.lower()
        if any(k in low for k in ["yaml parse error", "yaml:", "error converting yaml"]):
            return "yaml_syntax"
        if any(k in low for k in ["did not find expected", "mapping values"]):
            return "yaml_structure"
        if "unexpected" in low:
            return "template_syntax"
        if "undefined" in low or "not defined" in low:
            return "template_reference"
        if "nil pointer" in low:
            return "nil_reference"
        if "cannot range" in low:
            return "type_error"
        return "other"

    # ── public API (mirrors Reporter interface) ────────────────────────

    def print_header(self, title: str):
        pass  # no-op in JSON mode

    def print_chart_info(self, chart_templates: ChartTemplates):
        self._data["summary"] = {
            "chart_path": str(chart_templates.chart_path),
            "template_files": chart_templates.total_files,
            "total_blocks": chart_templates.total_blocks,
            "files": [
                {
                    "name": t.file_path.name,
                    "blocks": len(t.blocks),
                    "lines": t.total_lines,
                }
                for t in chart_templates.templates
            ],
        }

    def print_search_mode(self, mode: SearchMode):
        self._data["search"]["mode"] = mode.value

    def print_step_progress(self, step: SearchStep):
        self._data["steps"].append({
            "step": step.step_number,
            "type": "block",
            "range": step.blocks_tested,
            "passed": step.passed,
        })

    def print_file_step_progress(self, file_name: str, step: SearchStep):
        self._data["steps"].append({
            "step": step.step_number,
            "type": "block",
            "file": file_name,
            "range": step.blocks_tested,
            "passed": step.passed,
        })

    def print_search_result(self, result: SearchResult):
        if not result.found_error:
            self._data["status"] = "success"
            self._data["exit_code"] = 0
            return

        self._populate_block_error(result)

    def _populate_block_error(self, result: SearchResult):
        block = result.failing_block
        template = result.template

        lines = template.original_content.splitlines()
        context_before = []
        context_after = []
        start = max(0, block.start_line - 4)
        for i in range(start, block.start_line - 1):
            if i < len(lines):
                context_before.append({"line": i + 1, "content": lines[i]})
        for i in range(block.end_line, min(len(lines), block.end_line + 3)):
            context_after.append({"line": i + 1, "content": lines[i]})

        failing_lines = []
        for ln in range(block.start_line, block.end_line + 1):
            if ln - 1 < len(lines):
                failing_lines.append({"line": ln, "content": lines[ln - 1]})

        error_message = ""
        if result.error_result:
            error_message = result.error_result.error_message or result.error_result.stderr

        self._data["status"] = "failure"
        self._data["exit_code"] = 1
        self._data["error"] = {
            "file": template.file_path.name,
            "line": block.start_line,
            "end_line": block.end_line,
            "block_type": block.block_type.value,
            "block_index": result.failing_block_index,
            "last_successful_block_index": result.last_successful_block_index,
            "category": self._error_category(error_message),
            "risk": self._risk_level(error_message),
            "helm_error": error_message.strip(),
            "failing_lines": failing_lines,
            "context_before": context_before,
            "context_after": context_after,
        }

        self._data["search"]["total_steps"] = len(result.steps) if result.steps else 0

        # rendered manifest snippet
        self._populate_rendered_context(result)

    def _populate_rendered_context(self, result):
        last_result = getattr(result, "last_successful_result", None)
        if not last_result or not last_result.stdout:
            return

        template = getattr(result, "template", None)
        if not template:
            return

        import re
        file_name = template.file_path.name
        section = self._extract_file_section(last_result.stdout, file_name)
        if not section:
            section = last_result.stdout

        rendered_lines = section.splitlines()
        if rendered_lines:
            total = len(rendered_lines)
            start = max(0, total - 5)
            snippet = [
                {"line": start + i + 1, "content": l}
                for i, l in enumerate(rendered_lines[start:])
            ]
            self._data["error"]["rendered_manifest_tail"] = snippet

    def _extract_file_section(self, rendered_output: str, file_name: str) -> Optional[str]:
        import re
        if not rendered_output:
            return None
        sections = re.split(r'^---\s*$', rendered_output, flags=re.MULTILINE)
        for section in sections:
            header_match = re.search(
                r'#\s*Source:\s*\S+/templates/' + re.escape(file_name),
                section,
            )
            if header_match:
                lines = section.splitlines()
                content_lines = []
                past_header = False
                for line in lines:
                    if past_header:
                        content_lines.append(line)
                    elif re.match(r'#\s*Source:', line):
                        past_header = True
                if content_lines:
                    return "\n".join(content_lines)
                return section.strip()
        return None

    def print_multi_file_results(self, results: dict[str, SearchResult]):
        # handled per-file via print_search_result
        pass

    def print_no_helm_error(self, result):
        self._data["status"] = "success"
        self._data["exit_code"] = 0

    def print_error(self, message: str):
        self._data["status"] = "error"
        self._data["exit_code"] = 2
        self._data["error"] = {"message": message}

    def print_warning(self, message: str):
        self._data.setdefault("warnings", []).append(message)

    def print_info(self, message: str):
        if self.verbose:
            self._data.setdefault("info", []).append(message)

    def print_suggestions(self, result: SearchResult):
        self._collect_suggestions(result)

    # ── Line-based search methods ──────────────────────────────────────

    def print_line_step_progress(self, step):
        self._data["steps"].append({
            "step": step.step_number,
            "type": "line",
            "range": step.lines_tested,
            "passed": step.passed,
        })

    def print_line_search_result(self, result):
        if not result.found_error:
            self._data["status"] = "success"
            self._data["exit_code"] = 0
            return

        self._populate_line_error(result)

    def _populate_line_error(self, result):
        template = result.template

        error_message = ""
        if result.error_result:
            error_message = result.error_result.error_message or result.error_result.stderr

        self._data["status"] = "failure"
        self._data["exit_code"] = 1
        self._data["error"] = {
            "file": template.file_path.name,
            "line": result.failing_line,
            "end_line": result.failing_line,
            "last_successful_line": result.last_successful_line,
            "category": self._error_category(error_message),
            "risk": self._risk_level(error_message),
            "helm_error": error_message.strip(),
            "failing_lines": [
                {"line": result.failing_line, "content": result.failing_content or ""}
            ],
            "context_before": [
                {"line": result.failing_line - len(result.context_before) + i, "content": c}
                for i, c in enumerate(result.context_before)
            ],
            "context_after": [
                {"line": result.failing_line + 1 + i, "content": c}
                for i, c in enumerate(result.context_after)
            ],
        }

        self._data["search"]["total_steps"] = len(result.steps) if result.steps else 0

        # rendered manifest snippet
        self._populate_rendered_context(result)

    def print_line_suggestions(self, result):
        self._collect_suggestions_from_line(result)

    # ── Suggestion collectors ──────────────────────────────────────────

    def _collect_suggestions(self, result: SearchResult):
        if not result.error_result:
            return
        error_msg = result.error_result.stderr.lower()
        s = []
        if "unexpected" in error_msg and "}" in error_msg:
            s.append("Check for mismatched or missing braces {{ }}")
        if "undefined" in error_msg:
            s.append("Verify the variable exists in values.yaml")
            s.append("Check for typos in variable names")
        if "not defined" in error_msg:
            s.append("The referenced template or helper may not exist")
        if "cannot range over" in error_msg:
            s.append("Ensure the value is a list or map before ranging")
        if "nil pointer" in error_msg:
            s.append("Add a nil check: {{- if .Values.something }}")
        self._data["suggestions"] = s

    def _collect_suggestions_from_line(self, result):
        if not result.error_result:
            return
        error_msg = result.error_result.stderr.lower()
        s = []
        if "yaml" in error_msg:
            if "indentation" in error_msg or "indent" in error_msg:
                s.append("Check indentation - YAML requires consistent spacing")
            if "did not find expected" in error_msg:
                s.append("Check YAML structure - possibly wrong indentation or missing item")
            if "mapping" in error_msg:
                s.append("Check YAML key-value syntax (key: value)")
        if "unexpected" in error_msg and "}" in error_msg:
            s.append("Check for mismatched or missing braces {{ }}")
        if "undefined" in error_msg:
            s.append("Verify the variable exists in values.yaml")
        self._data["suggestions"] = s

    # ── Flush ──────────────────────────────────────────────────────────

    def flush(self) -> int:
        """Print JSON to stdout and return the exit code."""
        print(json.dumps(self._data, indent=2, ensure_ascii=False))
        return self._data["exit_code"]
