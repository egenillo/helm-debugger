#!/usr/bin/env python3
"""
Helm Template Debugger

A tool to debug Helm template rendering issues by executing templates
incrementally to find the exact line causing failures.
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .engine import EXIT_TOOL_ERROR, DebugReport, Options, run
from .search import SearchMode


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog="helm-debug",
        description="Debug Helm template rendering issues by finding the exact line of failure.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  helm-debug ./my-chart
  helm-debug ./my-chart -f values-prod.yaml --set image.tag=v1.0.0
  helm-debug ./my-chart --watch
  helm-debug ./my-chart -o sarif > helm-debug.sarif
  helm-debug ./my-chart --kube-version 1.30.0 -a networking.k8s.io/v1/Ingress
  helm-debug ./my-chart -- --namespace prod   (args after -- go straight to helm template)

Exit codes: 0 = renders fine, 1 = template error found, 2 = tool error
        """
    )

    parser.add_argument("chart", help="Path to the Helm chart directory")

    values = parser.add_argument_group("values")
    values.add_argument(
        "-f", "--values", action="append", dest="values_files", default=[], metavar="FILE",
        help="Specify values file(s) to use (can be repeated)",
    )
    values.add_argument(
        "--set", action="append", dest="set_values", default=[], metavar="KEY=VALUE",
        help="Set values on the command line (can be repeated)",
    )

    search = parser.add_argument_group("search")
    search.add_argument(
        "-m", "--mode", choices=["binary", "step"], default="binary",
        help="Search mode: 'binary' (fast) or 'step' (detailed). Default: binary",
    )
    search.add_argument(
        "--file", dest="target_file", metavar="FILENAME",
        help="Only debug a specific template file (e.g. deployment.yaml or sub/svc.yaml)",
    )

    helm = parser.add_argument_group("helm")
    helm.add_argument(
        "-n", "--release-name", default="debug-release",
        help="Release name to use for helm template. Default: debug-release",
    )
    helm.add_argument("--helm-path", help="Path to helm executable (default: $HELM_BIN, then PATH)")
    helm.add_argument("--kube-version", metavar="VERSION", help="Kubernetes version for .Capabilities.KubeVersion")
    helm.add_argument(
        "-a", "--api-versions", action="append", default=[], metavar="API",
        help="Kubernetes API version for .Capabilities.APIVersions (can be repeated)",
    )
    helm.add_argument(
        "--dependency-update", action="store_true",
        help="Run 'helm dependency update' on a scratch copy before rendering",
    )
    helm.add_argument(
        "--timeout", type=int, default=60, metavar="SECONDS",
        help="Timeout for each helm invocation. Default: 60",
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "-o", "--output", choices=["text", "json", "sarif"], default="text",
        help="Output format: text (default), json (CI/CD) or sarif (GitHub code scanning)",
    )
    output.add_argument("-w", "--watch", action="store_true", help="Re-run whenever chart or values files change")
    output.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")
    output.add_argument("--no-color", action="store_true", help="Disable colored output (also honours NO_COLOR)")
    output.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    return parser


def split_passthrough(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split "... -- --flag value" into our arguments and raw helm arguments."""
    if "--" in argv:
        index = argv.index("--")
        return argv[:index], argv[index + 1:]
    return argv, []


def make_reporter(output: str, verbose: bool):
    if output == "json":
        from .json_reporter import JsonReporter
        return JsonReporter(verbose=verbose)
    if output == "sarif":
        from .sarif_reporter import SarifReporter
        return SarifReporter(verbose=verbose)
    from .text_reporter import TextReporter
    return TextReporter(verbose=verbose)


def main(argv: Optional[list[str]] = None) -> int:
    """Main entry point."""
    argv = list(sys.argv[1:] if argv is None else argv)
    own_args, helm_args = split_passthrough(argv)

    parser = create_parser()
    args = parser.parse_args(own_args)

    if args.watch and args.output != "text":
        parser.error("--watch only works with text output")

    from .text_reporter import configure_console
    configure_console(no_color=args.no_color or args.output != "text")

    options = Options(
        chart=args.chart,
        values_files=args.values_files,
        set_values=args.set_values,
        mode=SearchMode.BINARY if args.mode == "binary" else SearchMode.STEP_BY_STEP,
        target_file=args.target_file,
        release_name=args.release_name,
        helm_path=args.helm_path,
        kube_version=args.kube_version,
        api_versions=args.api_versions,
        dependency_update=args.dependency_update,
        helm_args=helm_args,
        timeout=args.timeout,
    )

    def run_once() -> int:
        reporter = make_reporter(args.output, args.verbose)
        try:
            report = run(options, reporter)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # never die with a traceback in CI
            report = DebugReport(chart_path=args.chart, status="error", message=f"Unexpected error: {e}")
        return reporter.finish(report)

    if not args.watch:
        try:
            return run_once()
        except KeyboardInterrupt:
            return EXIT_TOOL_ERROR

    from .watch import watch
    watched = [Path(args.chart)] + [Path(v) for v in args.values_files if Path(v).exists()]
    return watch(run_once, watched)


if __name__ == "__main__":
    sys.exit(main())
