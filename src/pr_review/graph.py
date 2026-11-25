"""Static import graph for a Python tree, built from source text alone.

Nothing is imported or executed, so this works on any commit without a
virtualenv. Edges point from the importing file to the imported file.

Known blind spots (reported in the output, not hidden): pytest fixtures,
importlib/string-based imports, Django settings and URL wiring, entry points,
and decorator registries. conftest.py gets special handling in analyze.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx

from pr_review import pyast


def _chain_name(path: str, pyset: set[str]) -> str:
    """Module name from the nearest ancestor that is not a regular package."""
    parts = path.split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    dirs = parts[:-1]
    i = len(dirs)
    while i > 0 and "/".join(dirs[:i] + ["__init__.py"]) in pyset:
        i -= 1
    name = dirs[i:] + ([] if stem == "__init__" else [stem])
    return ".".join(name)


def _alt_names(path: str) -> list[str]:
    parts = path.split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    full = parts[:-1] + ([] if stem == "__init__" else [stem])
    names = [".".join(full)]
    if full and full[0] in ("src", "lib", "python"):
        names.append(".".join(full[1:]))
    return [n for n in names if n]


@dataclass
class ImportGraph:
    g: nx.DiGraph
    index: dict[str, str]  # module name -> path
    module_of: dict[str, str]  # path -> primary module name
    parse_errors: list[str] = field(default_factory=list)
    unresolved: dict[str, set[str]] = field(default_factory=dict)  # dotted name -> importers
    trees: dict = field(default_factory=dict)  # path -> parsed ast.Module
    refs: dict = field(default_factory=dict)  # path -> list[pyast.ImportRef]
    exports: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)  # __init__ path -> bound -> (path, name)
    _anc: dict[str, set[str]] = field(default_factory=dict)

    def resolve(self, dotted: str) -> str | None:
        parts = dotted.split(".")
        while parts:
            hit = self.index.get(".".join(parts))
            if hit:
                return hit
            parts.pop()
        return None

    def direct_dependents(self, path: str) -> set[str]:
        return set(self.g.predecessors(path)) if path in self.g else set()

    def transitive_dependents(self, path: str) -> set[str]:
        if path not in self._anc:
            self._anc[path] = nx.ancestors(self.g, path) if path in self.g else set()
        return self._anc[path]

    def importers_of_module_name(self, dotted: str) -> set[str]:
        """Files whose imports point at `dotted` (or a submodule) but resolve nowhere."""
        out: set[str] = set()
        for name, paths in self.unresolved.items():
            if name == dotted or name.startswith(dotted + "."):
                out |= paths
        return out

    def importers_of_name(self, path: str, name: str) -> set[str]:
        """Files that do `from <module of path> import <name>`."""
        out = set()
        for src in self.direct_dependents(path):
            if name in self.g.edges[src, path].get("names", ()):
                out.add(src)
        return out


def build_graph(sources: dict[str, str], all_paths: set[str] | None = None) -> ImportGraph:
    """sources: path -> python source for the files whose imports become edges.
    all_paths: every .py path in the tree, used for module-name resolution.
    Defaults to the keys of `sources` (the whole tree was parsed)."""
    pyset = set(all_paths or ()) | set(sources)
    index: dict[str, str] = {}
    module_of: dict[str, str] = {}
    for path in sorted(pyset, key=lambda p: (p.count("/"), p)):
        primary = _chain_name(path, pyset)
        module_of[path] = primary
        if primary:
            index.setdefault(primary, path)
    for path in sorted(pyset):
        for alt in _alt_names(path):
            index.setdefault(alt, path)

    g = nx.DiGraph()
    g.add_nodes_from(pyset)
    errors: list[str] = []
    graph = ImportGraph(g, index, module_of, errors)

    parsed: dict[str, tuple] = {}
    for path, src in sources.items():
        tree = pyast.parse(src)
        if tree is None:
            errors.append(path)
            continue
        parsed[path] = (tree, pyast.imports(tree, module_of[path], path.endswith("__init__.py")))
        graph.trees[path] = tree
        graph.refs[path] = parsed[path][1]

    # Re-exports: `from .core import Option` inside pkg/__init__.py means
    # `from pkg import Option` (or `pkg.Option`) really depends on pkg/core.py.
    exports = graph.exports
    for path, (_, refs) in parsed.items():
        if not path.endswith("__init__.py"):
            continue
        table = exports.setdefault(path, {})
        for ref in refs:
            if ref.is_from and ref.module:
                target = graph.resolve(ref.module)
                if target and target != path:
                    for name, bound in zip(ref.names, ref.bound):
                        table.setdefault(bound, (target, name))

    for path, (tree, refs) in parsed.items():
        uses = None
        for ref in refs:
            names: tuple[str, ...] = ()
            if ref.is_from:
                plain = []
                for n in ref.names:
                    sub = index.get(f"{ref.module}.{n}" if ref.module else n)
                    if sub and sub != path:
                        _add(g, path, sub, ())
                    else:
                        plain.append(n)
                        # could be an attribute or a submodule that no longer exists
                        full = f"{ref.module}.{n}" if ref.module else n
                        graph.unresolved.setdefault(full, set()).add(path)
                names = tuple(plain)
                if not ref.module or (not plain and ref.names):
                    continue
            elif ref.bound and ("." not in ref.module or ref.bound[0] != ref.module.split(".")[0]):
                # `import pkg as p` / `import mod`: record which attributes are used
                if uses is None:
                    uses = pyast.attribute_uses(tree)
                names = tuple(sorted(uses.get(ref.bound[0], ())))
            target = graph.resolve(ref.module) if ref.module else None
            if target and target != path:
                _add(g, path, target, names)
                via = exports.get(target)
                if via:
                    for n in names:
                        dest = via.get(n, (None,))[0]
                        if dest and dest != path:
                            _add(g, path, dest, (n,))
            elif not target and ref.module:
                graph.unresolved.setdefault(ref.module, set()).add(path)
    return graph


def _add(g: nx.DiGraph, a: str, b: str, names: tuple[str, ...]) -> None:
    if g.has_edge(a, b):
        g.edges[a, b]["names"].update(names)
    else:
        g.add_edge(a, b, names=set(names))
