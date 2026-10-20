"""Generate a small, dependency-free PDF for an approved workflow report."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import textwrap
from typing import Any


_PAGE_LINES = 48
_LINE_WIDTH = 92


def _plain_lines(title: str, sections: list[tuple[str, str]]) -> list[str]:
    lines = [title, ""]
    for heading, content in sections:
        lines.extend((heading, "=" * min(len(heading), 72), ""))
        for raw_line in str(content or "").replace("\t", "  ").splitlines():
            raw_line = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw_line)
            if not raw_line.strip():
                lines.append("")
                continue
            wrapped = textwrap.wrap(
                raw_line,
                width=_LINE_WIDTH,
                break_long_words=True,
                break_on_hyphens=False,
                replace_whitespace=True,
                drop_whitespace=True,
            )
            lines.extend(wrapped or [""])
        lines.append("")
    return lines


def _pdf_literal(text: str) -> bytes:
    encoded = text.encode("cp1252", errors="replace")
    return b"(" + encoded.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def render_pdf(title: str, sections: list[tuple[str, str]]) -> bytes:
    """Render plain text sections into a valid PDF using built-in Helvetica."""
    lines = _plain_lines(title, sections)
    pages = [lines[index:index + _PAGE_LINES] for index in range(0, len(lines), _PAGE_LINES)] or [[]]

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # Filled with the page tree after page object ids are known.
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    page_ids = [4 + index * 2 for index in range(len(pages))]
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii")

    for page_id, page_lines in zip(page_ids, pages):
        content_id = page_id + 1
        drawing = [b"BT", b"/F1 10 Tf", b"50 744 Td", b"14 TL"]
        for line in page_lines:
            drawing.extend((_pdf_literal(line) + b" Tj", b"T*"))
        drawing.append(b"ET")
        stream = b"\n".join(drawing) + b"\n"
        page_object = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode("ascii")
        stream_object = f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"endstream"
        objects.extend((page_object, stream_object))

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")

    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


def write_run_documentation_pdf(
    workspace_root: str,
    run: dict[str, Any],
    written_paths: list[str],
) -> str:
    """Write agent documentation and run evidence as a PDF in the run branch."""
    root = Path(workspace_root).resolve()
    run_id = str(run.get("id") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("Invalid workflow run id for PDF documentation.")

    sections: list[tuple[str, str]] = [
        ("Request", str(run.get("message") or "")),
        ("Implementation summary", str(run.get("final_answer") or "")),
    ]

    stage_results = run.get("stage_results") or []
    sections.append((
        "Agent results",
        "\n".join(
            f"{stage.get('agent', 'agent')} — {stage.get('status', 'unknown')}: {stage.get('summary', '')}"
            for stage in stage_results
            if isinstance(stage, dict)
        ) or "No stage summaries were recorded.",
    ))

    validation_lines = []
    for event in run.get("tool_events") or []:
        if not isinstance(event, dict) or event.get("tool") != "repo.run_command":
            continue
        arguments = event.get("arguments") or {}
        result = event.get("result") or {}
        validation_lines.append(
            f"{arguments.get('command', '(command unavailable)')} — {event.get('status', 'unknown')}"
        )
        if isinstance(result, dict) and result.get("return_code") is not None:
            validation_lines.append(f"Exit code: {result['return_code']}")
    sections.append(("Validation", "\n".join(validation_lines) or "No validation commands were recorded."))

    markdown_docs: list[str] = []
    for relative in written_paths:
        candidate = (root / relative).resolve()
        if root not in candidate.parents or candidate.suffix.lower() not in {".md", ".txt"}:
            continue
        if candidate.is_file():
            try:
                content = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if content.strip():
                markdown_docs.append(f"Source: {relative}\n{content[:30000]}")
    sections.append(("Agent documentation", "\n\n".join(markdown_docs) or "No Markdown documentation was available."))
    sections.append(("Changed files", "\n".join(written_paths) or "No changed file list was recorded."))
    sections.append(("Workflow", f"Repository: {run.get('repository_slug') or 'unknown'}\nBranch: {run.get('branch_name') or 'pending'}\nGenerated: {datetime.now(timezone.utc).isoformat()}"))

    relative_pdf = f"docs/ai-dev-team-run-{run_id[:8]}.pdf"
    destination = (root / relative_pdf).resolve()
    if root not in destination.parents:
        raise ValueError("PDF path escaped the isolated workflow workspace.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(render_pdf("AI Dev Team — Engineering Documentation", sections))
    return relative_pdf
