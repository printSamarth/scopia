"""Optional language-server resolver.

Strictly additive: the tree-sitter graph is always drawn first and always correct to the
degree it claims. A language server only *sharpens* it, and a missing, slow or crashing
server must degrade to exactly today's behaviour — never to an error.
"""
