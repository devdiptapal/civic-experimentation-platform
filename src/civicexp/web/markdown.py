"""Render the platform's own Markdown readouts as HTML.

The readout and case summary are written once, as Markdown, by
:mod:`civicexp.report` -- the same text that is downloaded, archived, and
attached to a ticket. The web application shows that text rather than a
second rendering of the same result, so the page and the archived document
cannot disagree.

This handles exactly the subset the report writer produces: headings,
paragraphs, bullet lists, tables, block quotes, rules, and inline bold,
italic, and code. All text is escaped before any formatting is applied, so
plan text that happens to contain markup is shown, not executed.
"""

from __future__ import annotations

import html
import re

from .html import Markup

__all__ = ["markdown_to_html"]

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])_(.+?)_(?![\w])")
_CODE = re.compile(r"`([^`]+)`")


def _inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    codes: list[str] = []

    def stash(match: re.Match[str]) -> str:
        codes.append(f"<code>{match.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    escaped = _CODE.sub(stash, escaped)
    escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)
    escaped = _ITALIC.sub(r"<em>\1</em>", escaped)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], escaped)


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def markdown_to_html(text: str) -> Markup:
    lines = text.splitlines()
    out: list[str] = []
    paragraph: list[str] = []
    index = 0

    def flush() -> None:
        if paragraph:
            parts = []
            for i, raw in enumerate(paragraph):
                hard_break = raw.endswith("  ") and i < len(paragraph) - 1
                parts.append(_inline(raw.strip()) + ("<br>" if hard_break else ""))
            out.append("<p>" + " ".join(parts) + "</p>")
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            flush()
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            flush()
            # The page supplies the one h1; document headings start a level down.
            level = min(len(heading.group(1)) + 1, 6)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            index += 1
            continue
        if stripped == "---":
            flush()
            out.append("<hr>")
            index += 1
            continue
        if stripped.startswith(">"):
            flush()
            quoted = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quoted.append(lines[index].strip().lstrip(">").strip())
                index += 1
            out.append(f"<blockquote><p>{_inline(' '.join(quoted))}</p></blockquote>")
            continue
        if stripped.startswith("- "):
            flush()
            items = []
            while index < len(lines) and lines[index].strip().startswith("- "):
                items.append(f"<li>{_inline(lines[index].strip()[2:])}</li>")
                index += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        if (
            stripped.startswith("|")
            and index + 1 < len(lines)
            and re.match(r"^\|?\s*:?-{3,}", lines[index + 1].strip())
        ):
            flush()
            header = _cells(stripped)
            aligns = ["num" if c.endswith(":") else "" for c in _cells(lines[index + 1])]
            index += 2
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(_cells(lines[index]))
                index += 1

            def cls(i: int) -> str:
                return ' class="num"' if i < len(aligns) and aligns[i] else ""  # noqa: B023

            head = "".join(f'<th scope="col"{cls(i)}>{_inline(c)}</th>' for i, c in enumerate(header))
            def label(i: int) -> str:
                text = html.escape(re.sub(r"[*_`]", "", header[i]), quote=True) if i < len(header) else ""  # noqa: B023
                return f' data-label="{text}"'

            body = "".join(
                "<tr>"
                + "".join(f"<td{label(i)}{cls(i)}>{_inline(c)}</td>" for i, c in enumerate(row))
                + "</tr>"
                for row in rows
            )
            out.append(
                f'<div class="table-wrap"><table class="data"><thead><tr>{head}</tr></thead>'
                f"<tbody>{body}</tbody></table></div>"
            )
            continue
        paragraph.append(line)
        index += 1
    flush()
    return Markup("\n".join(out))
