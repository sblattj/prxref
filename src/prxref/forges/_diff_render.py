"""Render per-file diff entries as one git-style unified diff.

Shared by the adapters whose forge serves a merge request, a pull request or a
compare as a list of per-file entries rather than as raw diff text.
"""
from __future__ import annotations


def render_diff_entries(entries: list[dict]) -> str:
    """Render per-file diff entries as one git-style unified diff.

    Each entry is GitLab-shaped: ``old_path`` and ``new_path`` name the file on
    either side, the ``new_file``, ``deleted_file`` and ``renamed_file`` flags
    pick the header form, and ``diff`` holds the file's hunks without their
    headers. This rebuilds each file's ``diff --git`` header from those keys,
    then appends the hunks, so the parser downstream sees the same text shape
    a raw-diff endpoint returns. An entry with no ``diff`` (missing, ``None``
    or empty) is rendered as a header-only file. An empty list renders as
    ``""``.
    """
    diff_parts: list[str] = []
    for d in entries:
        old_path = d.get("old_path") or ""
        new_path = d.get("new_path") or ""
        new_file = d.get("new_file", False)
        deleted_file = d.get("deleted_file", False)
        renamed_file = d.get("renamed_file", False)
        raw_diff = d.get("diff") or ""

        header_lines = [f"diff --git a/{old_path} b/{new_path}"]
        if new_file:
            header_lines.append("new file mode 100644")
            header_lines.append("--- /dev/null")
            header_lines.append(f"+++ b/{new_path}")
        elif deleted_file:
            header_lines.append("deleted file mode 100644")
            header_lines.append(f"--- a/{old_path}")
            header_lines.append("+++ /dev/null")
        elif renamed_file:
            header_lines.append(f"rename from {old_path}")
            header_lines.append(f"rename to {new_path}")
            header_lines.append(f"--- a/{old_path}")
            header_lines.append(f"+++ b/{new_path}")
        else:
            header_lines.append(f"--- a/{old_path}")
            header_lines.append(f"+++ b/{new_path}")

        file_unified = "\n".join(header_lines)
        if raw_diff:
            if not raw_diff.startswith("\n"):
                file_unified += "\n"
            file_unified += raw_diff
            if not file_unified.endswith("\n"):
                file_unified += "\n"
        else:
            file_unified += "\n"

        diff_parts.append(file_unified)

    return "".join(diff_parts)
