"""Unified-diff parsing. Input is always `git diff --unified=0`, so hunks are exact."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_HEADER = re.compile(r"^diff --git a/(.*) b/(.*)$")

ADDED = "added"
DELETED = "deleted"
MODIFIED = "modified"


@dataclass
class FileChange:
    path: str
    old_path: str
    status: str = MODIFIED
    new_lines: list[int] = field(default_factory=list)
    old_lines: list[int] = field(default_factory=list)

    @property
    def renamed(self) -> bool:
        return self.old_path != self.path


def parse_diff(text: str) -> list[FileChange]:
    changes: list[FileChange] = []
    current: FileChange | None = None

    for line in text.splitlines():
        header = _DIFF_HEADER.match(line)
        if header:
            old_path, new_path = header.group(1), header.group(2)
            current = FileChange(path=new_path, old_path=old_path)
            changes.append(current)
            continue

        if current is None:
            continue

        if line.startswith("--- "):
            if line[4:].strip() == "/dev/null":
                current.status = ADDED
            continue
        if line.startswith("+++ "):
            if line[4:].strip() == "/dev/null":
                current.status = DELETED
            continue

        hunk = _HUNK.match(line)
        if hunk:
            old_start, old_count, new_start, new_count = (
                int(hunk.group(1)),
                1 if hunk.group(2) is None else int(hunk.group(2)),
                int(hunk.group(3)),
                1 if hunk.group(4) is None else int(hunk.group(4)),
            )
            current.new_lines.extend(range(new_start, new_start + new_count))
            current.old_lines.extend(range(old_start, old_start + old_count))

    return [c for c in changes if c.new_lines or c.old_lines or c.status != MODIFIED]
