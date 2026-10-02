# Contributing

## Adding a language

This is the contribution that matters most, and it is deliberately small: **one file**.

Everything outside `scopia/lang/` is language-neutral. The index, the graph builder
and all four renderers only ever see `FileFacts`, so a new language never requires a
change above the seam.

An adapter implements one method:

```python
class MyLangAdapter:
    name = "mylang"
    extensions = (".ml",)

    def extract(self, source: bytes) -> FileFacts:
        ...
```

`FileFacts` holds three things, all defined in `scopia/lang/base.py`:

| Field | What it is |
|---|---|
| `defs` | every named definition, with its line range and a path-independent `qualname` |
| `calls` | every call site, recorded by the **name** being called |
| `bases` | classes naming a base class or implemented interface |

Two rules matter:

1. **Record calls by name, don't resolve them.** Resolution happens in the index, where
   the whole repo is visible. An adapter that guesses which definition a call refers to
   is doing the wrong job in the wrong place.
2. **Keep `qualname` path-independent** (`Class.method`, not `src/a.py::Class.method`).
   The fact cache is keyed by git blob SHA, so identical content anywhere in the repo
   reuses the same entry.

Then register it in `scopia/lang/__init__.py::_registry`, and add a fixture repo under
`tests/fixtures/` covering the four shapes (`py_repo.py` is the reference).

A language that needs more than one grammar registers one instance per grammar and
gives them all the same `name` — `react.py` does this for `.js`, `.ts` and `.tsx`.
`name` is what stops a call crossing a language boundary, so two grammars parsing one
language must agree on it, or every import between them is dropped as a false edge.

Give the adapter a `grammar` attribute naming its tree-sitter grammar. Watch mode uses it
to tell a finished file from a half-written one: tree-sitter never raises on bad input, so
without it a syntax error looks identical to "no symbols".

Grammars should ship pre-built abi3 wheels. A dependency that needs a C toolchain at
install time breaks the "download and run" promise for exactly the people this tool is
for.

## Running things

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check .
```

## What this tool is not

Pull requests that turn scopia into a linter, a bug finder, or a code-quality scorer
will be declined. It maps what changed so a human reads less; it does not pass judgement
on the code. Output wording follows from that — "differs from the other five", never
"flagged" or "suspicious".

Similarly, an edge whose target is uncertain must fan out to every candidate and say so.
Picking the likeliest one produces a graph that looks confident and is sometimes wrong,
which is worse than one that is visibly unsure.
