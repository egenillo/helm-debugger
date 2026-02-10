# Helm Template Debugger

A Python tool to debug Helm template rendering issues by executing templates incrementally to find the exact line causing failures.

## What's New

### Rendered Manifest Output

When the debugger finds the failing line or block, it now displays the **rendered manifest** (after Go template expansion) around the failure point. This lets you see the actual expanded values, indentation, and YAML structure — not just the raw template source.

```
Rendered Manifest (before failure):
----------------------------------------
      14 |       labels:
      15 |         app: debug-release
      16 |     spec:
      17 |       containers:
      18 |         - name: debug-release
----------------------------------------
  (Helm reports error at rendered line 18)
```

This is especially useful for:
- Seeing what `.Values` expressions actually resolved to
- Checking whether `nindent`, `toYaml`, or other formatting functions produced the expected indentation
- Understanding the rendered line number Helm reports vs. the source template line

## Features

- **Smart Error Detection**: Automatically detects error type (YAML vs Go template) and uses appropriate search strategy
- **Auto-targeting**: Extracts the failing file from Helm's error message and focuses debugging there
- **Line-based Search**: For YAML indentation/structure errors, uses line-by-line binary search
- **Block-based Search**: For Go template errors, uses template block binary search
- **Binary Search Mode**: Quickly finds errors using binary search algorithm (O(log n))
- **Step-by-Step Mode**: Tests incrementally for detailed debugging
- **Helper File Handling**: Automatically skips `_helpers.tpl` files to avoid breaking template dependencies
- **Color-coded Output**: Clear visual feedback on success/failure
- **Cross-platform**: Works on Windows and Linux

## Requirements

- Python 3.9+
- Helm installed (or path to helm executable)

## Installation

No installation needed - just clone or copy the files:

```
helm_debug/
├── main.py           # CLI entry point
├── parser.py         # Template parsing logic
├── executor.py       # Helm command execution
├── searcher.py       # Block-based search algorithms
├── line_searcher.py  # Line-based search algorithms
├── reporter.py       # Output formatting
└── utils.py          # Helper utilities
```

## Usage

### Basic Usage

```bash
python main.py ./my-chart
```

### With Custom Values File

```bash
python main.py ./my-chart -f values-prod.yaml
python main.py ./my-chart -f values.yaml -f override.yaml
```

### Step-by-Step Mode (Detailed)

```bash
python main.py ./my-chart --mode step
```

### Debug Specific Template File

```bash
python main.py ./my-chart --file deployment.yaml
```

### Set Values Inline

```bash
python main.py ./my-chart --set image.tag=v1.0.0
python main.py ./my-chart --set replicas=3 --set debug=true
```

### Custom Helm Path

```bash
python main.py ./my-chart --helm-path /usr/local/bin/helm
python main.py ./my-chart --helm-path C:\tools\helm.exe
```

### Verbose Output

```bash
python main.py ./my-chart -v
```

## Command-Line Options

| Option | Description |
|--------|-------------|
| `chart` | Path to Helm chart directory (required) |
| `-f, --values FILE` | Specify values file(s) (can be repeated) |
| `--set KEY=VALUE` | Set values on command line (can be repeated) |
| `-m, --mode {binary\|step}` | Search mode: binary (fast) or step (detailed) |
| `--file FILENAME` | Only debug specific template file |
| `-n, --release-name NAME` | Release name for helm template (default: debug-release) |
| `--helm-path PATH` | Path to helm executable (auto-detected if not set) |
| `-v, --verbose` | Enable verbose output |
| `--no-color` | Disable colored output |

## Example Output

### YAML Indentation Error (Line-based Search)

```
Chart Analysis
--------------------------------------------------
  Chart path:     openprodoc
  Template files: 20
  Total blocks:   2327

Info: Initial error: YAML parse error on templates/ollama-deployment.yaml
Info: Helm reports error in: ollama-deployment.yaml (line 66)
Info: Auto-targeting file: ollama-deployment.yaml

[Binary Search Mode]
Info: Detected YAML error - using line-based search
  Testing lines 1-69... FAIL
  Testing lines 1-34... FAIL
  Testing lines 1-17... OK
  Testing lines 1-25... OK
  Testing lines 1-32... OK
  Testing lines 1-33... OK

=======================================================
  ERROR FOUND: ollama-deployment.yaml:34
=======================================================

----------------------------------------
     31 |         image: "{{ .Values.ollama.image.repository }}:{{ .Values.ollama.image.tag }}"
     32 |         imagePullPolicy: {{ .Values.ollama.image.pullPolicy }}
     33 |       command:
>>   34 |         - sh
     35 |         - -c
     36 |         - |
----------------------------------------

Helm Error:
----------------------------------------
  Error: YAML parse error: yaml: line 83: did not find expected '-' indicator

Details:
  Failing line: 34
  Last successful line: 33

Search completed in 8 steps

Suggestions:
  - Check YAML structure - possibly wrong indentation or missing item
```

### Go Template Error (Block-based Search)

```
Chart Analysis
--------------------------------------------------
  Chart path:     test-chart
  Template files: 3
  Total blocks:   57

[Binary Search Mode]
  Testing blocks 0-7... OK
  Testing blocks 0-11... OK
  Testing blocks 0-13... FAIL
  Testing blocks 0-12... OK

=======================================================
  ERROR FOUND: configmap.yaml:14
=======================================================

----------------------------------------
     13 |   database.conf: |
>>   14 |     host: {{ .Values.database.host }
     15 |     port: {{ .Values.database.port }}
----------------------------------------

Helm Error:
----------------------------------------
  Error: parse error at (configmap.yaml:14): unexpected "}" in operand

Suggestions:
  - Check for mismatched or missing braces {{ }}
```

## How It Works

1. **Initial Analysis**: Runs `helm template` to capture the initial error message

2. **Error Classification**:
   - YAML errors (indentation, structure) -> Line-based search
   - Go template errors (syntax, undefined variables) -> Block-based search

3. **Auto-targeting**: Parses Helm's error message to extract the failing file name and line number

4. **Smart File Selection**:
   - Skips helper files (`_helpers.tpl`) that define shared templates
   - Focuses on the file identified in the error message

5. **Binary Search**:
   - For YAML errors: Bisects source lines while preserving template control structures
   - For template errors: Bisects template blocks (if/end, range/end, etc.)

6. **Error Reporting**: Shows the exact failing line with context and suggestions

## Error Types Handled

### YAML Errors
- Indentation problems
- Missing list indicators (-)
- Invalid mapping syntax
- Structure errors

### Go Template Errors
- Missing closing braces `}}`
- Undefined variables
- Invalid function calls
- Mismatched control structures

## Notes

- **No Cluster Needed**: `helm template` is a client-side operation that doesn't require a Kubernetes cluster
- **Safe Operation**: Creates temporary copies of your chart - never modifies originals
- **Preserves Dependencies**: Keeps helper templates intact when debugging other files
- **Cross-platform**: Works on Windows (cmd, PowerShell) and Linux/macOS terminals

## Troubleshooting

### Helm Not Found
```
Error: Helm executable not found. Install Helm or use --helm-path.
```
**Solution**: Install Helm or use `--helm-path` to specify location

### No Chart.yaml
```
Error: No Chart.yaml found in: ./my-chart
```
**Solution**: Ensure you're pointing to a valid Helm chart directory

### Helper Template Errors
If you see errors about missing templates like `"openprodoc.fullname"`:
- The debugger automatically skips `_helpers.tpl` files
- If manually targeting with `--file _helpers.tpl`, the helper definitions may break other templates

### Encoding Issues (Windows)
If you see garbled characters, the tool automatically handles UTF-8 encoding. If issues persist:
```bash
python main.py ./my-chart --no-color
```

