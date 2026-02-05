"""
Template parser for Helm template files.
Parses Go template syntax and breaks templates into debuggable blocks.
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from utils import read_file_content


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
class ParsedTemplate:
    """Represents a fully parsed template file."""
    file_path: Path
    blocks: list[TemplateBlock] = field(default_factory=list)
    original_content: str = ""

    @property
    def total_lines(self) -> int:
        if not self.original_content:
            return 0
        return len(self.original_content.splitlines())


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
        "comment": re.compile(r'\{\{/\*.*?\*/\}\}', re.DOTALL),
        "expression": re.compile(r'\{\{-?.*?-?\}\}', re.DOTALL),
    }

    def parse_file(self, file_path: Path) -> ParsedTemplate:
        """Parse a single template file into blocks."""
        content = read_file_content(file_path)
        blocks = self._parse_content(content, file_path)

        return ParsedTemplate(
            file_path=file_path,
            blocks=blocks,
            original_content=content
        )

    def _parse_content(self, content: str, file_path: Path) -> list[TemplateBlock]:
        """Parse content string into template blocks."""
        blocks = []
        lines = content.splitlines(keepends=True)
        current_line = 1
        nesting_level = 0

        i = 0
        while i < len(lines):
            line = lines[i]
            line_stripped = line.strip()

            # Skip empty lines - include them in next block
            if not line_stripped:
                i += 1
                current_line += 1
                continue

            # Detect block type from line content
            block_type = self._detect_block_type(line)

            # Handle nesting level changes
            if block_type in (BlockType.IF, BlockType.RANGE, BlockType.WITH, BlockType.DEFINE):
                block = TemplateBlock(
                    block_type=block_type,
                    content=line,
                    start_line=current_line,
                    end_line=current_line,
                    file_path=file_path,
                    nesting_level=nesting_level
                )
                blocks.append(block)
                nesting_level += 1

            elif block_type == BlockType.END:
                nesting_level = max(0, nesting_level - 1)
                block = TemplateBlock(
                    block_type=block_type,
                    content=line,
                    start_line=current_line,
                    end_line=current_line,
                    file_path=file_path,
                    nesting_level=nesting_level
                )
                blocks.append(block)

            elif block_type in (BlockType.ELSE, BlockType.ELSE_IF):
                # else/else if are at same level as their if
                block = TemplateBlock(
                    block_type=block_type,
                    content=line,
                    start_line=current_line,
                    end_line=current_line,
                    file_path=file_path,
                    nesting_level=max(0, nesting_level - 1)
                )
                blocks.append(block)

            else:
                # Plain content or expressions
                block = TemplateBlock(
                    block_type=block_type,
                    content=line,
                    start_line=current_line,
                    end_line=current_line,
                    file_path=file_path,
                    nesting_level=nesting_level
                )
                blocks.append(block)

            i += 1
            current_line += 1

        return blocks

    def _detect_block_type(self, line: str) -> BlockType:
        """Detect the type of template block from a line."""
        stripped = line.strip()

        # Check for specific patterns in order of specificity
        if self.PATTERNS["comment"].search(stripped):
            return BlockType.COMMENT

        if self.PATTERNS["else_if"].search(stripped):
            return BlockType.ELSE_IF

        if self.PATTERNS["else"].search(stripped):
            return BlockType.ELSE

        if self.PATTERNS["end"].search(stripped):
            return BlockType.END

        if self.PATTERNS["if_start"].search(stripped):
            return BlockType.IF

        if self.PATTERNS["range_start"].search(stripped):
            return BlockType.RANGE

        if self.PATTERNS["with_start"].search(stripped):
            return BlockType.WITH

        if self.PATTERNS["define_start"].search(stripped):
            return BlockType.DEFINE

        if self.PATTERNS["template"].search(stripped):
            return BlockType.TEMPLATE

        if self.PATTERNS["include"].search(stripped):
            return BlockType.INCLUDE

        if self.PATTERNS["expression"].search(stripped):
            return BlockType.EXPRESSION

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


def parse_chart(chart_path: str) -> ChartTemplates:
    """Parse all template files in a Helm chart."""
    from utils import get_template_files

    chart_path = Path(chart_path)
    template_files = get_template_files(str(chart_path))

    parser = TemplateParser()
    templates = []

    for file_path in template_files:
        parsed = parser.parse_file(file_path)
        if parsed.blocks:  # Only include files with content
            templates.append(parsed)

    return ChartTemplates(chart_path=chart_path, templates=templates)
