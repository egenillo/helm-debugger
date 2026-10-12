# Helm Template Debugger

Find the **exact template line** that breaks `helm template` — instead of squinting at
`Error: YAML parse error ... line 83`. No cluster needed; your chart is never modified.

```
$ python main.py ./my-chart
  ERROR FOUND: deployment.yaml:11
>>   11 |   d: {{ .Values.image.tagg.x }}

Helm Error:
  Error: template: my-chart/templates/deployment.yaml:11:15: ... nil pointer evaluating interface {}.x

Values Diagnosis:
  .Values.image.tagg is not defined (.Values.image has: tag). Did you mean .Values.image.tag?
```

## What's new in 2.0

- **Values diagnostics** — for `.Values.x.y` failures, rebuilds the merged values (chart defaults, `-f`, `--set`, subchart scoping) and tells you which key is missing, with "did you mean" hints. Needs PyYAML (`pip install pyyaml`); skipped otherwise.
- **SARIF output** (`-o sarif`) — failing line shows up as an inline annotation on pull requests via GitHub code scanning.
- **Watch mode** (`--watch`) — re-runs whenever the chart or values files change.
- **Helm plugin** — `helm debug ./my-chart`.
- **More Helm flags** — `--kube-version`, `--api-versions`, `--dependency-update`, and raw pass-through after `--`.
- **Subcharts and nested template folders** (`templates/sub/x.yaml`, `charts/child/templates/...`) are now found and debugged.
- **Faster** — the chart is copied once per session instead of once per step.

### Bugs fixed

- Text mode always exited `0`; it now exits `1` when an error is found.
- Go template errors in `.yaml` files were misclassified as YAML errors (file names like `cm.yaml:11` matched `yaml:`).
- A parse error *after* an `{{ if }}` block was reported at the `if`. Truncated templates now auto-close open `if`/`range`/`with` blocks.
- Unclosed `{{ if }}` blocks are located directly from the template structure.
- Multi-line `{{ ... }}` actions and comments are never cut in half.
- Relative `-f` paths broke because helm ran from a temp directory.
- Errors inside `_helpers.tpl` are located from Helm's own line number instead of by truncating the helpers.

## Install

Python 3.9+ and Helm 3 required.

```bash
python main.py ./my-chart

# or install it as a command
pip install .            # then: helm-debugger ./my-chart
pip install ".[values]"  # same, plus PyYAML for values diagnostics

# optional, for values diagnostics:
pip install pyyaml

# as a Helm plugin
helm plugin install /path/to/helm-debugger
helm debug ./my-chart
```

Helm is found via `--helm-path`, then `$HELM_BIN` (set by Helm for plugins), then `PATH`.

## Usage

```bash
python main.py ./my-chart
python main.py ./my-chart -f values-prod.yaml --set image.tag=v1.0.0
python main.py ./my-chart --file sub/svc.yaml        # one template only
python main.py ./my-chart --mode step                # test every line (slower, more detail)
python main.py ./my-chart --watch                    # re-run on every save
python main.py ./my-chart --kube-version 1.30.0 -a networking.k8s.io/v1/Ingress
python main.py ./my-chart --dependency-update
python main.py ./my-chart -- --namespace prod        # everything after -- goes to helm template
```

| Option | Description |
|--------|-------------|
| `chart` | Path to the chart directory |
| `-f, --values FILE` | Values file (repeatable) |
| `--set KEY=VALUE` | Inline value (repeatable) |
| `-m, --mode {binary,step}` | Binary search (default) or line by line |
| `--file NAME` | Only debug this template (`x.yaml`, `sub/x.yaml`) |
| `-n, --release-name` | Release name (default `debug-release`) |
| `--helm-path PATH` | Helm executable |
| `--kube-version V` | Passed to `helm template --kube-version` |
| `-a, --api-versions API` | Passed to `helm template --api-versions` (repeatable) |
| `--dependency-update` | Run `helm dependency update` on the scratch copy |
| `--timeout SECONDS` | Per-helm-call timeout (default 60) |
| `-o, --output {text,json,sarif}` | Output format |
| `-w, --watch` | Re-run on file changes (text output only) |
| `-v, --verbose` | Extra info |
| `--no-color` | Plain output (also honours `NO_COLOR`) |

**Exit codes:** `0` renders fine · `1` template error found · `2` tool error (bad path, Helm missing, ...).

## How it works

1. Runs `helm template`; if it succeeds, done.
2. Reads the failing file from Helm's message and renders growing **prefixes** of that template
   (lines `1..N`, the rest blanked so line numbers stay stable). Open `{{ if }}`/`{{ range }}`
   blocks are closed automatically so each prefix is valid, and cuts never land inside a
   multi-line action.
3. Binary-searches for the smallest `N` that fails — that is your line (`O(log n)` renders).
4. Shows the code, the rendered manifest just before the failure, Helm's error, a values
   diagnosis, and fix suggestions.

Shortcuts skip the search when the answer is already known: unclosed blocks (from the template
structure) and `_helpers.tpl` (from Helm's reported line).

## CI/CD

### JSON

```bash
python main.py ./my-chart -o json > helm-debug-report.json
```

Key fields: `status` (`success`/`failure`/`error`), `exit_code`, `error.file`, `error.line`,
`error.category` (`yaml_structure`, `yaml_syntax`, `template_syntax`, `template_reference`,
`nil_reference`, `required_value`, `type_error`, `other`), `error.risk` (`high`/`medium`/`low`),
`error.helm_error`, `error.failing_lines`, `error.rendered_manifest_tail`,
`error.values_diagnosis`, `suggestions`, `steps`.

Schema version is `2.0`: `error.block_index` and `error.last_successful_block_index` were removed
(the search is now line-based); `error.file` uses the path under `templates/` (`sub/svc.yaml`).

### GitHub code scanning (SARIF)

```yaml
- run: pip install pyyaml
- run: python main.py ./chart -o sarif > helm-debug.sarif
  continue-on-error: true
- uses: github/codeql-action/upload-sarif@v3
  with:
    sarif_file: helm-debug.sarif
```

### Plain gate

```bash
python main.py ./chart -o json > report.json || true
jq -e '.status != "failure"' report.json
```

## License

See [LICENSE](LICENSE).
