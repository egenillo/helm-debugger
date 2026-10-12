"""
Template parser for Helm template files.
Parses Go template syntax, breaks templates into debuggable blocks and
analyses the control structure so templates can be safely truncated.
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from .utils import read_file_content, get_template_files


class BlockType(Enum):
    """Types of template blocks."""
    PLAIN = "plain"           # Plain YAML content
    EXPRESSION = "expression"  # {{ .Values.something }}
    IF = "if"                 # {{- if ... }}
    ELSE = "else"             # {{- else }}
    ELSE_IF = "else_if"       # {{- else if ... }}
    END = "end"               # {{- end }}
    RANGE = "range"           # {{- range ... }}
    WITH = "with"             # {{- with ... }}
    DEFINE = "define"         # {{- define ... }}
    TEMPLATE = "template"     # {{- template ... }}
    INCLUDE = "include"       # {{- include ... }}
    COMMENT = "comment"       # {{/* ... */}}


# Keywords that open a scope which must be closed by {{ end }}
SCOPE_OPENERS = ("if", "range", "with", "define", "block")

# A Go template action. Comments are matched first because they may contain "}}".
ACTION_RE = re.compile(
    r'\{\{-?\s*/\*.*?\*/\s*-?\}\}'   # {{/* comment */}}
    r'|\{\{.*?\}\}',                 # {{ action }}
    re.DOTALL,
)
KEYWORD_RE = re.compile(r'\{\{-?\s*([a-z]+)\b')


@dataclass
class TemplateBlock:
    """Represents a block of template content."""
    block_type: BlockType
    content: str
    start_line: int
    end_line: int
    file_path: Optional[Path] = None
    nesting_level: int = 0

    def __str__(self):
        return f"{self.block_type.value}@{self.start_line}-{self.end_line}"


@dataclass
class Action:
    """A single {{ ... }} action located in a template."""
    keyword: str      # "if", "end", "else", ... or "" for expressions
    start_line: int
    end_line: int
    is_comment: bool = False


@dataclass
class TemplateStructure:
    """Control-flow facts about a template, used to build valid truncations."""
    total_lines: int
    cut_points: list[int]           # lines after which the template can be cut
    open_after: dict[int, int]      # cut line -> scopes still open at that point
    unclosed_openers: list[int]     # lines of openers that are never closed


@dataclass
class ParsedTemplate:
    """Represents a fully parsed template file."""
    file_path: Path
    blocks: list[TemplateBlock] = field(default_factory=list)
    original_content: str = ""
    rel_path: str = ""   # POSIX path relative to the root chart, e.g. "templates/cm.yaml"
    _structure: Optional[TemplateStructure] = field(default=None, repr=False)

    @property
    def total_lines(self) -> int:
        if not self.original_content:
            return 0
        return len(self.original_content.splitlines())

    @property
    def display_name(self) -> str:
        """Short name for humans: "cm.yaml", "sub/svc.yaml" or "charts/x/templates/a.yaml"."""
        if self.rel_path.startswith("templates/"):
            return self.rel_path[len("templates/"):]
        return self.rel_path or self.file_path.name

    @property
    def is_helper(self) -> bool:
        """Helper files (_helpers.tpl etc.) only hold shared definitions."""
        return self.file_path.name.startswith("_")

    @property
    def lines(self) -> list[str]:
        return self.original_content.splitlines()

    @property
    def structure(self) -> TemplateStructure:
        if self._structure is None:
            self._structure = analyze_structure(self.original_content)
        return self._structure

    def matches(self, name: str) -> bool:
        """True if `name` refers to this template (rel path, display name or basename)."""
        name = name.replace("\\", "/")
        return name in (self.rel_path, self.display_name, self.file_path.name)

    def block_at_line(self, line: int) -> Optional[TemplateBlock]:
        for block in self.blocks:
            if block.start_line <= line <= block.end_line:
                return block
        return None

    def build_prefix(self, cut_line: int) -> str:
        """
        Return a version of the template that keeps lines 1..cut_line and blanks the rest.
        Scopes left open by the cut ({{ if }}, {{ range }}, ...) are closed with
        {{- end }} so the truncated template is still valid Go template syntax.
        Blank lines keep line numbers in Helm's error messages unchanged.
        """
        lines = self.original_content.splitlines(keepends=True)
        kept = lines[:cut_line]
        newline = "\r\n" if "\r\n" in self.original_content else "\n"
        blanked = [newline if line.endswith(("\n", "\r")) else "" for line in lines[cut_line:]]

        open_scopes = self.structure.open_after.get(cut_line, 0)
        closing = "{{- end }}" * open_scopes
        if closing and kept and not kept[-1].endswith(("\n", "\r")):
            closing = newline + closing

        return "".join(kept) + "".join(blanked) + closing


def scan_actions(content: str) -> list[Action]:
    """Find every {{ ... }} action with its line span and keyword."""
    actions = []
    for match in ACTION_RE.finditer(content):
        text = match.group(0)
        start_line = content.count("\n", 0, match.start()) + 1
        end_line = start_line + text.count("\n")
        is_comment = bool(re.match(r'\{\{-?\s*/\*', text))
        keyword = ""
        if not is_comment:
            kw = KEYWORD_RE.match(text)
            keyword = kw.group(1) if kw else ""
        actions.append(Action(keyword, start_line, end_line, is_comment))
    return actions


def analyze_structure(content: str) -> TemplateStructure:
    """Compute safe cut points, open scope counts and unclosed openers."""
    total = len(content.splitlines())
    actions = scan_actions(content)

    # Lines strictly inside a multi-line action cannot be used as cut points
    inside = set()
    for action in actions:
        inside.update(range(action.start_line, action.end_line))

    # Scope depth after each line, and a stack to spot unclosed openers
    delta_by_line: dict[int, list[Action]] = {}
    for action in actions:
        delta_by_line.setdefault(action.end_line, []).append(action)

    open_after = {}
    stack: list[int] = []
    for line in range(1, total + 1):
        for action in delta_by_line.get(line, []):
            if action.keyword in SCOPE_OPENERS:
                stack.append(action.start_line)
            elif action.keyword == "end" and stack:
                stack.pop()
        open_after[line] = len(stack)

    cut_points = [line for line in range(1, total + 1) if line not in inside]
    return TemplateStructure(
        total_lines=total,
        cut_points=cut_points,
        open_after=open_after,
        unclosed_openers=list(stack),
    )


class TemplateParser:
    """Parses Helm template files into blocks."""

    # Regex patterns for template constructs
    PATTERNS = {
        "if_start": re.compile(r'\{\{-?\s*if\s+'),
        "else_if": re.compile(r'\{\{-?\s*else\s+if\s+'),
        "else": re.compile(r'\{\{-?\s*else\s*-?\}\}'),
        "end": re.compile(r'\{\{-?\s*end\s*-?\}\}'),
        "range_start": re.compile(r'\{\{-?\s*range\s+'),
        "with_start": re.compile(r'\{\{-?\s*with\s+'),
        "define_start": re.compile(r'\{\{-?\s*define\s+'),
        "template": re.compile(r'\{\{-?\s*template\s+'),
        "include": re.compile(r'\{\{-?\s*include\s+'),
        "comment": re.compile(r'\{\{-?\s*/\*.*?\*/\s*-?\}\}', re.DOTALL),
        "expression": re.compile(r'\{\{-?.*?-?\}\}', re.DOTALL),
    }

    def parse_file(self, file_path: Path, rel_path: str = "") -> ParsedTemplate:
        """Parse a single template file into blocks."""
        content = read_file_content(file_path)
        blocks = self._parse_content(content, file_path)

        return ParsedTemplate(
            file_path=file_path,
            blocks=blocks,
            original_content=content,
            rel_path=rel_path or f"templates/{file_path.name}",
        )

    def _parse_content(self, content: str, file_path: Path) -> list[TemplateBlock]:
        """Parse content into one block per non-empty line."""
        blocks = []
        nesting_level = 0

        for line_num, line in enumerate(content.splitlines(), start=1):
            if not line.strip():
                continue

            block_type = self._detect_block_type(line)

            if block_type == BlockType.END:
                nesting_level = max(0, nesting_level - 1)
                level = nesting_level
            elif block_type in (BlockType.ELSE, BlockType.ELSE_IF):
                # else/else if are at same level as their if
                level = max(0, nesting_level - 1)
            else:
                level = nesting_level

            blocks.append(TemplateBlock(
                block_type=block_type,
                content=line,
                start_line=line_num,
                end_line=line_num,
                file_path=file_path,
                nesting_level=level,
            ))

            if block_type in (BlockType.IF, BlockType.RANGE, BlockType.WITH, BlockType.DEFINE):
                nesting_level += 1

        return blocks

    def _detect_block_type(self, line: str) -> BlockType:
        """Detect the type of template block from a line."""
        stripped = line.strip()

        # Check for specific patterns in order of specificity
        ordered = [
            ("comment", BlockType.COMMENT),
            ("else_if", BlockType.ELSE_IF),
            ("else", BlockType.ELSE),
            ("end", BlockType.END),
            ("if_start", BlockType.IF),
            ("range_start", BlockType.RANGE),
            ("with_start", BlockType.WITH),
            ("define_start", BlockType.DEFINE),
            ("template", BlockType.TEMPLATE),
            ("include", BlockType.INCLUDE),
            ("expression", BlockType.EXPRESSION),
        ]
        for key, block_type in ordered:
            if self.PATTERNS[key].search(stripped):
                return block_type

        return BlockType.PLAIN


@dataclass
class ChartTemplates:
    """Collection of all parsed templates in a chart."""
    chart_path: Path
    templates: list[ParsedTemplate] = field(default_factory=list)

    @property
    def all_blocks(self) -> list[TemplateBlock]:
        """Get all blocks from all templates."""
        blocks = []
        for template in self.templates:
            blocks.extend(template.blocks)
        return blocks

    @property
    def total_blocks(self) -> int:
        return len(self.all_blocks)

    @property
    def total_files(self) -> int:
        return len(self.templates)

    def find(self, name: str) -> Optional[ParsedTemplate]:
        """Find a template by rel path, display name or basename."""
        for template in self.templates:
            if template.matches(name):
                return template
        return None


def parse_chart(chart_path: str) -> ChartTemplates:
    """Parse all template files in a Helm chart, including unpacked subcharts."""
    parser = TemplateParser()
    templates = []

    for file_path, rel_path in get_template_files(chart_path):
        parsed = parser.parse_file(file_path, rel_path)
        if parsed.blocks:  # Only include files with content
            templates.append(parsed)

    return ChartTemplates(chart_path=Path(chart_path), templates=templates)
