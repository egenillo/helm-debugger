"""
JSON reporter for Helm template debugger.
Produces structured, machine-readable output for CI/CD pipeline integration.
"""

import json
import sys

from . import diagnostics
from .engine import DebugReport, Finding, Listener

SCHEMA_VERSION = "2.0"


def _lines(pairs) -> list[dict]:
    return [{"line": n, "content": c} for n, c in pairs]


def finding_to_dict(finding: Finding) -> dict:
    before, after = finding.context()
    data = {
        "file": finding.file,
        "path": finding.template.rel_path,
        "line": finding.line,
        "end_line": finding.end_line,
        "block_type": finding.block_type,
        "last_successful_line": finding.last_successful_line,
        "located_by": finding.located_by,
        "category": finding.category,
        "risk": finding.risk,
        "helm_error": finding.helm_error.strip(),
        "failing_lines": _lines(finding.failing_lines()),
        "context_before": _lines(before),
        "context_after": _lines(after),
        "values_diagnosis": finding.values.to_dict() if finding.values else None,
    }
    if finding.rendered_tail:
        data["rendered_manifest_tail"] = _lines(finding.rendered_tail)
    if finding.rendered_error_line:
        data["rendered_error_line"] = finding.rendered_error_line
    return data


def report_to_dict(report: DebugReport) -> dict:
    data = {
        "version": SCHEMA_VERSION,
        "status": report.status,
        "exit_code": report.exit_code,
        "summary": {},
        "search": {},
        "error": None,
        "suggestions": [],
        "steps": [
            {"step": i + 1, "type": "line", "range": s.lines_tested, "passed": s.passed}
            for i, s in enumerate(report.steps)
        ],
    }

    if report.chart is not None:
        data["summary"] = {
            "chart_path": str(report.chart.chart_path),
            "template_files": report.chart.total_files,
            "total_blocks": report.chart.total_blocks,
            "files": [
                {"name": t.display_name, "path": t.rel_path, "blocks": len(t.blocks), "lines": t.total_lines}
                for t in report.chart.templates
            ],
        }

    if report.mode is not None:
        data["search"] = {
            "mode": report.mode.value,
            "strategy": report.strategy,
            "total_steps": len(report.steps),
            "helm_renders": report.helm_renders,
        }

    if report.status == "error":
        data["error"] = {"message": report.message}
    elif report.finding:
        data["error"] = finding_to_dict(report.finding)
        data["suggestions"] = report.finding.suggestions
    elif report.status == "failure":
        data["error"] = {
            "file": None,
            "line": None,
            "category": report.category,
            "risk": diagnostics.risk_level(report.category),
            "helm_error": report.helm_error.strip(),
        }
        data["suggestions"] = diagnostics.suggestions_for(report.helm_error)

    if report.warnings:
        data["warnings"] = report.warnings
    return data


class JsonReporter(Listener):
    """Collects nothing live; prints one JSON document when the session ends."""

    def __init__(self, verbose: bool = False, out=None):
        self.verbose = verbose
        self.out = out or sys.stdout

    def finish(self, report: DebugReport) -> int:
        data = report_to_dict(report)
        if self.verbose:
            data["info"] = report.info
        print(json.dumps(data, indent=2, ensure_ascii=False), file=self.out)
        return report.exit_code
