"""Python AST helpers: formatting-only detection, public API diffs, risk
signals on added lines, and import extraction."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass


def parse(src: str | None) -> ast.Module | None:
    if src is None:
        return None
    try:
        return ast.parse(src)
    except (SyntaxError, ValueError):
        return None


def same_ast(old_src: str | None, new_src: str | None) -> bool:
    """True when two sources differ only in formatting or comments."""
    a, b = parse(old_src), parse(new_src)
    if a is None or b is None:
        return False
    return ast.dump(a) == ast.dump(b)


# ---------------------------------------------------------------- public API

def _is_public(name: str, all_names: set[str] | None) -> bool:
    if all_names is not None:
        return name in all_names
    return not name.startswith("_")


def _all_names(tree: ast.Module) -> set[str] | None:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                return {e.value for e in node.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return None


def _sig(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    decos = "".join(f"@{ast.unparse(d)} " for d in fn.decorator_list)
    prefix = "async def" if isinstance(fn, ast.AsyncFunctionDef) else "def"
    ret = f" -> {ast.unparse(fn.returns)}" if fn.returns is not None else ""
    return f"{decos}{prefix} {fn.name}({ast.unparse(fn.args)}){ret}"


def _shape(node: ast.AST):
    """What callers depend on: parameter names, order, and which are optional."""
    if isinstance(node, ast.ClassDef):
        return ("class", {_dotted(b.value if isinstance(b, ast.Subscript) else b) for b in node.bases})
    a = node.args
    pos = [x.arg for x in a.posonlyargs + a.args]
    n_req = len(pos) - len(a.defaults)
    kw = {x.arg: d is not None for x, d in zip(a.kwonlyargs, a.kw_defaults)}
    return ("def", pos, n_req, kw, a.vararg is not None, a.kwarg is not None)


def _compatible(old, new) -> bool:
    """True if every call that worked against `old` still works against `new`."""
    if old[0] != new[0]:
        return False
    if old[0] == "class":
        return old[1] <= new[1]
    _, opos, oreq, okw, ovar, okwarg = old
    _, npos, nreq, nkw, nvar, nkwarg = new
    if npos[: len(opos)] != opos and not (nvar and npos == opos[: len(npos)]):
        return False
    if nreq > oreq:
        return False
    if any(k not in nkw and not nkwarg for k in okw):
        return False
    if any(not has_default for k, has_default in nkw.items() if k not in okw):
        return False
    return (nvar or not ovar) and (nkwarg or not okwarg)


def public_api(src: str | None, shapes: dict | None = None) -> dict[str, str]:
    tree = parse(src)
    if tree is None:
        return {}
    all_names = _all_names(tree)
    api: dict[str, str] = {}

    def keep(name: str, node: ast.AST, text: str) -> None:
        api[name] = text
        if shapes is not None:
            shapes[name] = _shape(node)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_public(node.name, all_names):
            keep(node.name, node, _sig(node))
        elif isinstance(node, ast.ClassDef) and _is_public(node.name, all_names):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            keep(node.name, node, f"class {node.name}({bases})")
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    not item.name.startswith("_") or item.name in ("__init__", "__call__")
                ):
                    keep(f"{node.name}.{item.name}", item, _sig(item))
    return api


def api_changes(old_src: str | None, new_src: str | None) -> list[dict]:
    """Public API diff. `compatible` marks changes existing callers survive,
    such as a new optional parameter or a changed annotation."""
    os_, ns = {}, {}
    old, new = public_api(old_src, os_), public_api(new_src, ns)
    out = []
    for name in sorted(old.keys() | new.keys()):
        if name not in new:
            out.append({"name": name, "change": "removed", "old": old[name], "new": None, "compatible": False})
        elif name not in old:
            out.append({"name": name, "change": "added", "old": None, "new": new[name], "compatible": True})
        elif old[name] != new[name]:
            out.append({"name": name, "change": "changed", "old": old[name], "new": new[name],
                        "compatible": _compatible(os_[name], ns[name])})
    return out


# ---------------------------------------------------------------- risk signals

@dataclass(frozen=True)
class SignalKind:
    label: str
    weight: int


SIGNALS: dict[str, SignalKind] = {
    "hardcoded_secret": SignalKind("possible hard-coded secret", 15),
    "dynamic_exec": SignalKind("eval/exec", 12),
    "shell": SignalKind("shell command", 10),
    "unsafe_deserialize": SignalKind("unsafe deserialization", 8),
    "debugger": SignalKind("debugger left in", 8),
    "bare_except": SignalKind("bare except", 7),
    "broad_except": SignalKind("broad except", 5),
    "mutable_default": SignalKind("mutable default argument", 6),
    "sql_string": SignalKind("SQL built from a string", 6),
    "new_async": SignalKind("new async function", 3),
    "global_stmt": SignalKind("global statement", 3),
    "type_ignore": SignalKind("type: ignore", 2),
    "noqa": SignalKind("lint suppressed", 1),
    "todo": SignalKind("TODO/FIXME", 1),
}

_LINE_PATTERNS = [
    ("hardcoded_secret", re.compile(
        r"""(?i)\b(password|passwd|secret|api_?key|access_?key|token|private_?key)\w*\s*[:=]\s*['"][^'"\s]{8,}['"]""")),
    ("type_ignore", re.compile(r"#\s*type:\s*ignore")),
    ("noqa", re.compile(r"#\s*noqa|#\s*pylint:\s*disable|#\s*pragma:\s*no cover")),
    ("todo", re.compile(r"#.*\b(TODO|FIXME|HACK|XXX)\b")),
    ("sql_string", re.compile(r"""(?i)f['"]\s*(select|insert|update|delete)\s.*\{""")),
]


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _kw(call: ast.Call, name: str) -> ast.expr | None:
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def _mutable(node: ast.expr | None) -> bool:
    if isinstance(node, (ast.List, ast.Dict, ast.Set)):
        return True
    return isinstance(node, ast.Call) and _dotted(node.func) in ("list", "dict", "set")


def scan_signals(src: str | None, added: set[int], is_test: bool = False) -> list[dict]:
    """Risk signals that appear on added lines only."""
    if not src or not added:
        return []
    found: dict[tuple[int, str], str] = {}
    lines = src.splitlines()
    for ln in sorted(added):
        if 0 < ln <= len(lines):
            text = lines[ln - 1]
            for kind, rx in _LINE_PATTERNS:
                if kind == "hardcoded_secret" and is_test:
                    continue
                if rx.search(text):
                    found[(ln, kind)] = text.strip()[:160]

    tree = parse(src)
    if tree is not None and not is_test:
        for node in ast.walk(tree):
            ln = getattr(node, "lineno", None)
            if ln not in added:
                continue
            if isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    found[(ln, "bare_except")] = "except:"
                elif _dotted(node.type) in ("Exception", "BaseException"):
                    found[(ln, "broad_except")] = f"except {_dotted(node.type)}"
            elif isinstance(node, ast.Call):
                name = _dotted(node.func)
                if name in ("eval", "exec", "compile"):
                    found[(ln, "dynamic_exec")] = f"{name}(...)"
                elif name in ("os.system", "os.popen", "commands.getoutput"):
                    found[(ln, "shell")] = f"{name}(...)"
                elif name.startswith("subprocess.") and isinstance(_kw(node, "shell"), ast.Constant) and _kw(node, "shell").value:
                    found[(ln, "shell")] = f"{name}(..., shell=True)"
                elif name in ("pickle.load", "pickle.loads", "marshal.loads", "dill.loads"):
                    found[(ln, "unsafe_deserialize")] = f"{name}(...)"
                elif name == "yaml.load" and _kw(node, "Loader") is None:
                    found[(ln, "unsafe_deserialize")] = "yaml.load without Loader"
                elif name in ("breakpoint", "pdb.set_trace", "ipdb.set_trace"):
                    found[(ln, "debugger")] = f"{name}()"
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
                if any(_mutable(d) for d in defaults):
                    found[(ln, "mutable_default")] = f"def {node.name}(...)"
                if isinstance(node, ast.AsyncFunctionDef):
                    found[(ln, "new_async")] = f"async def {node.name}"
            elif isinstance(node, ast.Global):
                found[(ln, "global_stmt")] = "global " + ", ".join(node.names)

    return [
        {"line": ln, "kind": kind, "detail": f"{SIGNALS[kind].label}: {detail}"}
        for (ln, kind), detail in sorted(found.items())
    ]


# ---------------------------------------------------------------- migrations

def migration_issue(src: str | None) -> str | None:
    """Spot migrations that cannot be rolled back."""
    tree = parse(src)
    if tree is None:
        return None
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    if "upgrade" in funcs:  # Alembic
        down = funcs.get("downgrade")
        if down is None:
            return "Alembic migration has no downgrade()"
        body = [s for s in down.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        if all(isinstance(s, ast.Pass) for s in body):
            return "Alembic downgrade() is empty"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _dotted(node.func).endswith("RunPython"):
            if len(node.args) < 2 and _kw(node, "reverse_code") is None:
                return "Django RunPython has no reverse_code"
        if isinstance(node, ast.Call) and _dotted(node.func).endswith("RunSQL"):
            if len(node.args) < 2 and _kw(node, "reverse_sql") is None:
                return "Django RunSQL has no reverse_sql"
    return None


# ---------------------------------------------------------------- imports

@dataclass
class ImportRef:
    module: str  # absolute dotted target
    names: tuple[str, ...]  # names pulled in with `from x import ...`
    is_from: bool
    bound: tuple[str, ...] = ()  # local names this statement binds (asname or name)


def imports(tree: ast.Module, module_name: str, is_package: bool) -> list[ImportRef]:
    out: list[ImportRef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import a.b` binds `a`; `import a.b as x` binds `x` to a.b
                bound = alias.asname or alias.name.split(".")[0]
                out.append(ImportRef(alias.name, (), False, (bound,)))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                pkg = module_name.split(".") if module_name else []
                if not is_package:
                    pkg = pkg[:-1]
                up = node.level - 1
                if up > len(pkg):
                    continue
                pkg = pkg[: len(pkg) - up]
                target = ".".join(pkg + (node.module.split(".") if node.module else []))
            else:
                target = node.module or ""
            kept = [a for a in node.names if a.name != "*"]
            out.append(ImportRef(target, tuple(a.name for a in kept), True, tuple(a.asname or a.name for a in kept)))
    return out


def attribute_uses(tree: ast.Module) -> dict[str, set[str]]:
    """`click.Option` -> {"click": {"Option"}} for every Name.attr in the file."""
    uses: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            uses.setdefault(node.value.id, set()).add(node.attr)
    return uses
