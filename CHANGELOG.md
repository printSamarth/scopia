# Changelog

## 1.0.0 — 2026-10-02

First release.

- Diff → touched-symbol graph for Python, PHP and React
- One-line changes are first-class: the unit of analysis is the enclosing symbol, and
  nodes are weighted by fan-in rather than diff size
- Every edge carries provenance; a call through an unknown receiver is never presented
  as certain, and uncertain edges state why
- PHP resolves the class a call site names — `Service::class`, `new Service()`,
  `Service::method()`, typed properties, and inherited methods
- Python resolves aliased imports (`from x import run as v3`)
- React reads `.jsx`, `.tsx`, `.js` and `.ts` as one language: `<Button />` is a call
  edge, `const`/`memo`/`forwardRef`/`useCallback` components are definitions, a default
  import under another local name still resolves, and a declared TypeScript type
  narrows a call to the class it names
- Untracked files are reviewed; `git diff` cannot see them
- Files in unsupported languages are listed rather than silently omitted
- Repo index cached per git blob SHA inside `.git/`, so warm runs re-parse nothing
- `--utility` hides widely-called plumbing; `--exclude` skips paths
- Text, mermaid, JSON, and a self-contained clickable HTML report
- A gate that declines to draw a graph when the diff is too small to need one
- `--watch`: redraws as files settle (debounced, polled from `git status` with no
  file-watching dependency), holds the last good graph while a file does not parse,
  skips redraws that change nothing, and marks what arrived since the last redraw
- `--watch --html`: a live page served on 127.0.0.1 that patches itself in place,
  keeping the selected node and scroll position across redraws
- Java: `.java`, parsed through tree-sitter with no JDK; receivers resolve through
  declared parameter, field and local types
- `--lsp`: optional language-server resolution (Python via `jedi-language-server`).
  Confirmed edges are marked `resolved`; a definition outside the repo drops the
  name-matched edges. Never required, never an error when absent
- `--to-entry`: climbs callers until each chain reaches something nothing in the repo
  calls — the route, job or command that triggers the changed code
- Fixed: a long call chain in a large uncapped graph exhausted the recursion limit
