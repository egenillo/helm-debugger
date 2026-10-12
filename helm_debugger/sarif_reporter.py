"""
SARIF 2.1.0 reporter.
Upload the output with github/codeql-action/upload-sarif to get the failing
template line annotated directly on pull requests (GitHub code scanning).
"""

import json
import sys
from pathlib import Path

from . import __version__, diagnostics
from .engine import DebugReport, Listener
from .utils import find_chart_yaml

SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
RULE_IDS = list(diagnostics.CATEGORY_DESCRIPTIONS)


def _artifact_uri(path: Path) -> str:
    """Repository-relative URI when the file is under the working directory, else file:// URI."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return resolved.as_uri()


def _rules() -> list[dict]:
    return [
        {
            "id": rule_id,
            "name": "".join(part.title() for part in rule_id.split("_")),
            "shortDescription": {"text": description},
            "defaultConfiguration": {"level": "error"},
            "properties": {"risk": diagnostics.risk_level(rule_id)},
        }
        for rule_id, description in diagnostics.CATEGORY_DESCRIPTIONS.items()
    ]


def _result(report: DebugReport) -> dict:
    finding = report.finding
    category = report.category
    suggestions = finding.suggestions if finding else diagnostics.suggestions_for(report.helm_error)

    text = (finding.helm_error if finding else report.helm_error).strip() or "Helm failed to render the chart"
    if finding and finding.values:
        text += f"\n\n{finding.values.message}"
    if suggestions:
        text += "\n\nSuggestions:\n" + "\n".join(f"- {s}" for s in suggestions)

    if finding and finding.line:
        location = {
            "artifactLocation": {"uri": _artifact_uri(finding.template.file_path)},
            "region": {"startLine": finding.line, "endLine": finding.end_line or finding.line},
        }
    else:
        # Code scanning needs a location: fall back to the chart's Chart.yaml
        chart_yaml = find_chart_yaml(Path(report.chart_path)) or Path(report.chart_path) / "Chart.yaml"
        location = {"artifactLocation": {"uri": _artifact_uri(chart_yaml)}, "region": {"startLine": 1}}

    return {
        "ruleId": category,
        "ruleIndex": RULE_IDS.index(category) if category in RULE_IDS else RULE_IDS.index("other"),
        "level": "error",
        "message": {"text": text},
        "locations": [{"physicalLocation": location}],
        "properties": {"risk": diagnostics.risk_level(category)},
    }


def report_to_sarif(report: DebugReport) -> dict:
    run = {
        "tool": {
            "driver": {
                "name": "helm-debug",
                "version": __version__,
                "rules": _rules(),
            }
        },
        "invocations": [{"executionSuccessful": report.status != "error"}],
        "results": [],
    }

    if report.status == "error":
        run["invocations"][0]["toolExecutionNotifications"] = [
            {"level": "error", "message": {"text": report.message}}
        ]
    elif report.status == "failure":
        run["results"].append(_result(report))

    return {"$schema": SARIF_SCHEMA, "version": "2.1.0", "runs": [run]}


class SarifReporter(Listener):
    """Prints one SARIF log when the session ends."""

    def __init__(self, verbose: bool = False, out=None):
        self.verbose = verbose
        self.out = out or sys.stdout

    def finish(self, report: DebugReport) -> int:
        print(json.dumps(report_to_sarif(report), indent=2, ensure_ascii=False), file=self.out)
        return report.exit_code
