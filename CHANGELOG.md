# Changelog

## 0.1.0 — unreleased

First release.

- Diff → touched-symbol graph for Python and PHP
- One-line changes are first-class: the unit of analysis is the enclosing symbol, and
  nodes are weighted by fan-in rather than diff size
- Every edge carries provenance; a call through an unknown receiver is never presented
  as certain, and uncertain edges state why
- PHP resolves the class a call site names — `Service::class`, `new Service()`,
  `Service::method()`, typed properties, and inherited methods
- Python resolves aliased imports (`from x import run as v3`)
- Untracked files are reviewed; `git diff` cannot see them
- Files in unsupported languages are listed rather than silently omitted
- Repo index cached per git blob SHA inside `.git/`, so warm runs re-parse nothing
- `--utility` hides widely-called plumbing; `--exclude` skips paths
- Text, mermaid, JSON, and a self-contained clickable HTML report
- A gate that declines to draw a graph when the diff is too small to need one
