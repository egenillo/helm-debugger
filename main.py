#!/usr/bin/env python3
"""
Helm Template Debugger

A tool to debug Helm template rendering issues by executing templates
incrementally to find the exact line causing failures.
"""

import argparse
import sys
from pathlib import Path

from utils import validate_chart_directory, find_helm_executable
from parser import parse_chart
from executor import HelmExecutor, IncrementalExecutor
from searcher import BinarySearcher, StepByStepSearcher, SearchMode
from line_searcher import LineBinarySearcher, LineStepByStepSearcher
from reporter import Reporter


def is_yaml_error(error_message: str) -> bool:
    """Check if the error is a YAML parsing error (not a Go template error)."""
    yaml_indicators = [
        "yaml parse error",
        "yaml:",
        "error converting yaml to json",
        "did not find expected",
        "mapping values are not allowed",
        "could not find expected",
    ]
    error_lower = error_message.lower()
    return any(indicator in error_lower for indicator in yaml_indicators)


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog="helm-debug",
        description="Debug Helm template rendering issues by finding the exact line of failure.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  helm-debug ./my-chart
  helm-debug ./my-chart -f values-prod.yaml
  helm-debug ./my-chart --mode step
  helm-debug ./my-chart --set image.tag=v1.0.0
  helm-debug ./my-chart --file deployment.yaml
        """
    )

    parser.add_argument(
        "chart",
        help="Path to the Helm chart directory"
    )

    parser.add_argument(
        "-f", "--values",
        action="append",
        dest="values_files",
        default=[],
        metavar="FILE",
        help="Specify values file(s) to use (can be repeated)"
    )

    parser.add_argument(
        "--set",
        action="append",
        dest="set_values",
        default=[],
        metavar="KEY=VALUE",
        help="Set values on the command line (can be repeated)"
    )

    parser.add_argument(
        "-m", "--mode",
        choices=["binary", "step"],
        default="binary",
        help="Search mode: 'binary' (fast) or 'step' (detailed). Default: binary"
    )

    parser.add_argument(
        "--file",
        dest="target_file",
        metavar="FILENAME",
        help="Only debug a specific template file (e.g., deployment.yaml)"
    )

    parser.add_argument(
        "-n", "--release-name",
        default="debug-release",
        help="Release name to use for helm template. Default: debug-release"
    )

    parser.add_argument(
        "--helm-path",
        help="Path to helm executable (auto-detected if not specified)"
    )

    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose output"
    )

    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output"
    )

    return parser


def main():
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args()

    # Initialize reporter
    reporter = Reporter(verbose=args.verbose)

    # Disable colors if requested
    if args.no_color:
        from reporter import Colors
        Colors.disable()

    # Validate chart directory
    is_valid, error_msg = validate_chart_directory(args.chart)
    if not is_valid:
        reporter.print_error(error_msg)
        return 1

    # Find helm executable
    helm_path = args.helm_path or find_helm_executable()
    if not helm_path:
        reporter.print_error(
            "Helm executable not found. Install Helm or use --helm-path."
        )
        return 1

    if args.verbose:
        reporter.print_info(f"Using Helm at: {helm_path}")

    # Parse chart templates
    try:
        chart_templates = parse_chart(args.chart)
    except Exception as e:
        reporter.print_error(f"Failed to parse chart: {e}")
        return 1

    if chart_templates.total_files == 0:
        reporter.print_warning("No template files found in chart.")
        return 0

    reporter.print_chart_info(chart_templates)

    # Create executor
    try:
        executor = HelmExecutor(
            helm_path=helm_path,
            release_name=args.release_name,
            values_files=args.values_files,
            set_values=args.set_values
        )
    except RuntimeError as e:
        reporter.print_error(str(e))
        return 1

    # First, check if the full template has an error
    inc_executor = IncrementalExecutor(executor, args.chart)
    full_result = inc_executor.validate_full_template()

    if full_result.success:
        reporter.print_no_helm_error(full_result)
        return 0

    if args.verbose:
        reporter.print_info(f"Initial error: {full_result.error_message}")

    # Extract failing file from Helm error message
    failing_file = full_result.get_failing_file()
    failing_line = full_result.get_failing_line()

    if failing_file and args.verbose:
        reporter.print_info(f"Helm reports error in: {failing_file}" +
                          (f" (line {failing_line})" if failing_line else ""))

    # Filter templates to debug
    templates_to_debug = chart_templates.templates

    # If user specified a file, use that
    if args.target_file:
        templates_to_debug = [
            t for t in templates_to_debug
            if t.file_path.name == args.target_file
        ]
        if not templates_to_debug:
            reporter.print_error(f"Template file not found: {args.target_file}")
            return 1
    # Otherwise, use the file from Helm's error message
    elif failing_file:
        templates_to_debug = [
            t for t in templates_to_debug
            if t.file_path.name == failing_file
        ]
        if templates_to_debug:
            reporter.print_info(f"Auto-targeting file: {failing_file}")
        else:
            reporter.print_warning(f"Could not find template: {failing_file}")
            # Fall back to all templates, but skip helper files
            templates_to_debug = [
                t for t in chart_templates.templates
                if not t.file_path.name.startswith('_')
            ]
    else:
        # Skip helper files (like _helpers.tpl) from debugging
        templates_to_debug = [
            t for t in templates_to_debug
            if not t.file_path.name.startswith('_')
        ]
        if args.verbose:
            reporter.print_info("Skipping helper files (_*.tpl) from debugging")

    # Determine search mode
    mode = SearchMode.BINARY if args.mode == "binary" else SearchMode.STEP_BY_STEP
    reporter.print_search_mode(mode)

    # Check if this is a YAML error (use line-based search) or template error (use block-based)
    use_line_search = is_yaml_error(full_result.stderr)
    if use_line_search:
        reporter.print_info("Detected YAML error - using line-based search")

    # Search for errors in each template
    for template in templates_to_debug:
        if args.verbose:
            reporter.print_info(f"Debugging: {template.file_path.name}")

        if use_line_search:
            # Use line-based search for YAML errors
            def line_progress_callback(step):
                reporter.print_line_step_progress(step)

            if mode == SearchMode.BINARY:
                searcher = LineBinarySearcher(
                    executor, args.chart, line_progress_callback
                )
            else:
                searcher = LineStepByStepSearcher(
                    executor, args.chart, line_progress_callback
                )

            result = searcher.search(template)

            reporter.print_line_search_result(result)

            if result.found_error:
                reporter.print_line_suggestions(result)
                break
        else:
            # Use block-based search for template errors
            def progress_callback(step):
                reporter.print_step_progress(step)

            if mode == SearchMode.BINARY:
                searcher = BinarySearcher(
                    executor, args.chart, progress_callback
                )
            else:
                searcher = StepByStepSearcher(
                    executor, args.chart, progress_callback
                )

            result = searcher.search(template)

            reporter.print_search_result(result)

            if result.found_error:
                reporter.print_suggestions(result)
                break

    return 0


if __name__ == "__main__":
    sys.exit(main())
