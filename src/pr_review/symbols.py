"""Symbol index for the code a PR touches.

This is the cheap, name-based pass. It only looks at:
  * the changed files, at the merge-base and at head, to find which functions,
    methods and classes were added, removed or changed, and
  * the files that import a changed module, to find who uses those symbols.

Names are resolved through each file's imports (including package
re-exports), `module.attr` chains and `self.method` inside a class. Anything
it cannot pin down, such as `obj.method()` on an object of unknown type, is
kept as a low-confidence match. `pr-review refine` replaces these results with
jedi's, which follows types.

Symbol ids look like "path/to/file.py::Class.method".
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass

from pr_review import pyast
from pr_review.graph import ImportGraph

MAX_CALLERS = 200
MAX_LOW_CONFIDENCE = 40


@dataclass
class Sym:
    qualname: str
    kind: str  # function, method, class, module
    line: int
    end: int
    col: int  # column of the name token, for jedi
    sig: str
    shape: tuple | None
    digest: str
    node: ast.AST
    public: bool


def sym_id(path: str, qual: str) -> str:
    return f"{path}::{qual}"


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def _name_col(lines: list[str], node) -> int:
    kw = "class " if isinstance(node, ast.ClassDef) else "def "
    line = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
    i = line.find(kw + node.name, node.col_offset)
    return i + len(kw) if i >= 0 else node.col_offset


def define(tree: ast.Module, src: str) -> dict[str, Sym]:
    """Top-level functions and classes, plus methods one level down."""
    lines = src.splitlines()
    all_names = pyast._all_names(tree)
    out: dict[str, Sym] = {}
    module_stmts = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = Sym(node.name, "function", node.lineno, node.end_lineno, _name_col(lines, node),
                                 pyast._sig(node), pyast._shape(node), _digest(ast.dump(node)), node,
                                 pyast._is_public(node.name, all_names))
        elif isinstance(node, ast.ClassDef):
            cls_public = pyast._is_public(node.name, all_names)
            rest = [s for s in node.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))]
            head = "|".join(ast.dump(x) for x in node.bases + node.keywords + node.decorator_list)
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            out[node.name] = Sym(node.name, "class", node.lineno, node.end_lineno, _name_col(lines, node),
                                 f"class {node.name}({bases})", pyast._shape(node),
                                 _digest(head + ast.dump(ast.Module(body=rest, type_ignores=[]))), node, cls_public)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    q = f"{node.name}.{item.name}"
                    pub = cls_public and (not item.name.startswith("_") or item.name in ("__init__", "__call__"))
                    out[q] = Sym(q, "method", item.lineno, item.end_lineno, _name_col(lines, item),
                                 pyast._sig(item), pyast._shape(item), _digest(ast.dump(item)), item, pub)
        else:
            module_stmts.append(node)
    out["<module>"] = Sym("<module>", "module", 1, max(1, len(lines)), 0, "", None,
                          _digest(ast.dump(ast.Module(body=module_stmts, type_ignores=[]))), tree, False)
    return out


def changed_symbols(old: dict[str, Sym], new: dict[str, Sym]) -> list[tuple[str, Sym | None, Sym | None]]:
    """(change, old, new) for every symbol that differs. change is one of
    added, removed, signature (callers may be affected) or modified (body only)."""
    out = []
    for q in sorted(old.keys() | new.keys()):
        a, b = old.get(q), new.get(q)
        if a is None:
            out.append(("added", None, b))
        elif b is None:
            out.append(("removed", a, None))
        elif a.sig != b.sig:
            out.append(("signature", a, b))
        elif a.digest != b.digest:
            out.append(("modified", a, b))
    return out


# ---------------------------------------------------------------- resolution

class Resolver:
    """Resolves names and attribute chains in one file to symbol ids."""

    def __init__(self, path: str, tree: ast.Module, graph: ImportGraph, local: dict[str, Sym] | None = None):
        self.path = path
        self.graph = graph
        self.local = local if local is not None else define_light(tree)
        self.table: dict[str, tuple] = {}
        for ref in graph.refs.get(path) or pyast.imports(tree, graph.module_of.get(path, ""), path.endswith("__init__.py")):
            if ref.is_from:
                for name, bound in zip(ref.names, ref.bound):
                    sub = f"{ref.module}.{name}" if ref.module else name
                    if sub in graph.index:
                        self.table[bound] = ("mod", sub)
                    elif ref.module:
                        self.table[bound] = ("attr", ref.module, name)
            elif ref.bound:
                bound = ref.bound[0]
                # `import a.b` binds `a` (the top package); `import a.b as x` binds x to a.b
                plain = "." in ref.module and bound == ref.module.split(".")[0]
                self.table[bound] = ("mod", bound if plain else ref.module)

    def attr_of_module(self, dotted: str, name: str) -> tuple | None:
        sub = f"{dotted}.{name}"
        if sub in self.graph.index:
            return ("mod", sub)
        path = self.graph.index.get(dotted)
        if not path:
            return ("ext", sub)
        hit = self.graph.exports.get(path, {}).get(name)
        if hit:
            return ("sym", hit[0], hit[1])
        return ("sym", path, name)

    def expr(self, node, cls: str | None) -> tuple | None:
        if isinstance(node, ast.Name):
            n = node.id
            if n in ("self", "cls") and cls:
                return ("self", cls)
            entry = self.table.get(n)
            if entry:
                if entry[0] == "attr":
                    return self.attr_of_module(entry[1], entry[2])
                return entry
            if n in self.local and self.local[n].kind in ("function", "class"):
                return ("sym", self.path, n)
            return None
        if isinstance(node, ast.Attribute):
            base = self.expr(node.value, cls)
            if base is None:
                return None
            kind = base[0]
            if kind == "mod":
                return self.attr_of_module(base[1], node.attr)
            if kind == "self":
                return ("sym", self.path, f"{base[1]}.{node.attr}")
            if kind == "sym" and "." not in base[2]:
                return ("sym", base[1], f"{base[2]}.{node.attr}")
            if kind == "ext":
                return ("ext", f"{base[1]}.{node.attr}")
        return None


def define_light(tree: ast.Module) -> dict[str, Sym]:
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            out[node.name] = Sym(node.name, kind, node.lineno, node.end_lineno, 0, "", None, "", node, True)
    return out


def spans(tree: ast.Module) -> list[tuple[int, int, str, str | None]]:
    """(start, end, qualname, enclosing class) for defs, outermost first."""
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.lineno, node.end_lineno, node.name, None))
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append((item.lineno, item.end_lineno, f"{node.name}.{item.name}", node.name))
            out.append((node.lineno, node.end_lineno, node.name, node.name))
    return out


def enclosing(span_list, line: int) -> tuple[str | None, str | None]:
    for start, end, q, cls in span_list:
        if start <= line <= end:
            return q, cls
    return None, None


@dataclass
class Ref:
    target: str
    path: str
    line: int
    caller: str | None
    call: bool
    confidence: str  # high or low
    resolution: str  # name or jedi


def scan_refs(path: str, tree: ast.Module, resolver: Resolver, targets: set[str],
              methods_by_name: dict[str, set[str]]) -> list[Ref]:
    """References in one file to any id in `targets`."""
    span_list = spans(tree)
    call_funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    out: list[Ref] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Name, ast.Attribute)) or isinstance(getattr(node, "ctx", None), ast.Store):
            continue
        line = node.lineno
        caller, cls = enclosing(span_list, line)
        r = resolver.expr(node, cls)
        is_call = id(node) in call_funcs
        if r and r[0] == "sym":
            tid = sym_id(r[1], r[2])
            if tid in targets:
                out.append(Ref(tid, path, line, caller, is_call, "high", "name"))
                continue
        if isinstance(node, ast.Attribute) and node.attr in methods_by_name and (r is None or r[0] == "ext"):
            for tid in methods_by_name[node.attr]:
                out.append(Ref(tid, path, line, caller, is_call, "low", "name"))
    return out


def callees(sym: Sym, resolver: Resolver) -> set[str]:
    """Internal symbols called from inside `sym`."""
    cls = sym.qualname.split(".")[0] if sym.kind == "method" else None
    out = set()
    for n in ast.walk(sym.node):
        if isinstance(n, ast.Call):
            r = resolver.expr(n.func, cls)
            if r and r[0] == "sym":
                out.add(sym_id(r[1], r[2]))
    return out
