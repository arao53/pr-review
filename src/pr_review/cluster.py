"""Split the reviewable files into review units.

Files are linked by imports (tests to their subjects count double) and by
shared directories, then grouped with Louvain community detection. The
resolution is tuned toward roughly ten files per cluster, and small or
excess clusters are merged into their closest neighbour.
"""

from __future__ import annotations

import math
import posixpath
from collections import Counter

import networkx as nx

from pr_review.graph import ImportGraph


def _dir(p: str) -> str:
    return posixpath.dirname(p)


def _link_graph(paths: list[str], graph: ImportGraph, categories: dict[str, str]) -> nx.Graph:
    G = nx.Graph()
    G.add_nodes_from(paths)
    pset = set(paths)

    def bump(a: str, b: str, w: float) -> None:
        if G.has_edge(a, b):
            G.edges[a, b]["weight"] += w
        else:
            G.add_edge(a, b, weight=w)

    # Modules imported by many changed files (a shared `core`, `utils`) would
    # otherwise glue every cluster together, so their edges are damped.
    indeg = {p: sum(1 for q in graph.g.predecessors(p) if q in pset) for p in paths if p in graph.g}
    for a, b in graph.g.edges():
        if a in pset and b in pset:
            w = 2.0 if "test" in (categories.get(a), categories.get(b)) else 1.0
            bump(a, b, w / (1 + math.log2(1 + indeg.get(b, 0))))
    for p in paths:
        d = _dir(p)
        if categories.get(p) == "test":
            # tests/foo/test_x.py should sit with app/foo, not with other tests
            d = "/".join(x for x in d.split("/") if x not in ("tests", "test")) or d
        bump(p, f"dir:{d}", 1.0)
        while d:
            parent = _dir(d)
            bump(f"dir:{d}", f"dir:{parent}", 0.15)
            d = parent
    return G


def _affinity(a: set[str], b: set[str], G: nx.Graph) -> float:
    w = 0.0
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    for n in sorted(small):
        for m, data in sorted(G[n].items()):
            if m in big:
                w += data["weight"]
    da = Counter(_dir(p) for p in a).most_common(1)[0][0].split("/")
    db = Counter(_dir(p) for p in b).most_common(1)[0][0].split("/")
    common = 0
    for x, y in zip(da, db):
        if x != y:
            break
        common += 1
    return w + 0.3 * common


def cluster_paths(
    paths: list[str],
    graph: ImportGraph,
    categories: dict[str, str],
    sizes: dict[str, int] | None = None,
    max_clusters: int = 12,
    min_size: int = 2,
    line_budget: int = 2500,
) -> list[set[str]]:
    """Group paths into review units.

    line_budget is the rough number of changed lines one reviewer (or one
    subagent) should take in at once. Clusters above it are split along
    import lines; clusters far below it are folded into a neighbour.
    """
    if not paths:
        return []
    sizes = sizes or {p: 1 for p in paths}
    lines = lambda c: sum(sizes.get(p, 0) for p in c)  # noqa: E731
    total = lines(paths)
    if len(paths) <= 8 and total <= line_budget:
        return [set(paths)]

    G = _link_graph(paths, graph, categories)
    files = set(paths)
    target = max(2, min(max_clusters, round(len(paths) / 12), max(2, round(total / line_budget))))
    best: list[set[str]] | None = None
    for res in (0.5, 0.75, 1.0, 1.4, 2.0, 3.0, 4.5):
        comms = nx.community.louvain_communities(G, weight="weight", resolution=res, seed=7)
        groups = [set(c) & files for c in comms]
        groups = [g for g in groups if g]
        if best is None or abs(len(groups) - target) < abs(len(best) - target):
            best = groups
        if len(groups) >= target:
            break
    clusters: list[set[str]] = []
    for c in sorted(best or [files], key=min):
        clusters.extend(sorted(_split(c, G, lines, line_budget), key=min))

    min_lines = max(30, total * 0.01)

    def merge_smallest() -> None:
        clusters.sort(key=lambda c: (lines(c), len(c), min(c)))
        small = clusters.pop(0)

        def pick(i: int) -> float:
            a = _affinity(small, clusters[i], G)
            return a * (0.3 if lines(small) + lines(clusters[i]) > line_budget else 1.0)

        partner = max(range(len(clusters)), key=pick)
        clusters[partner] |= small

    while len(clusters) > 1 and any(len(c) < min_size or lines(c) < min_lines for c in clusters):
        merge_smallest()
    while len(clusters) > max_clusters:
        merge_smallest()
    return clusters


def _split(c: set[str], G: nx.Graph, lines, budget: int) -> list[set[str]]:
    """Break an oversized cluster along its own import structure."""
    if lines(c) <= budget * 1.3 or len(c) < 6:
        return [c]
    sub = nx.Graph()
    sub.add_nodes_from(sorted(c))
    for a in sorted(c):
        for b in sorted(G[a]):
            if b in c:
                sub.add_edge(a, b, weight=G[a][b]["weight"])
    want = max(2, round(lines(c) / budget))
    best = [c]
    for res in (1.0, 1.5, 2.2, 3.0, 4.5):
        parts = [set(p) for p in nx.community.louvain_communities(sub, weight="weight", resolution=res, seed=7)]
        if len(parts) > len(best):
            best = parts
        if len(parts) >= want:
            break
    return best


def cluster_name(paths: set[str]) -> str:
    dirs = [_dir(p) for p in paths]
    common = posixpath.commonpath(dirs) if all(dirs) else ""
    if common:
        return common
    top = Counter(d.split("/")[0] if d else "(root)" for d in dirs).most_common(2)
    if len(top) == 1:
        return top[0][0]
    return f"{top[0][0]} + {top[1][0]}"
