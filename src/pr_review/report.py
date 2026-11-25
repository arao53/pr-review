"""Write review.json and render it into a single offline HTML file."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

PLACEHOLDER = "/*__PR_REVIEW_DATA__*/"


def render_html(review: dict) -> str:
    template = resources.files("pr_review").joinpath("templates/report.html").read_text(encoding="utf-8")
    data = json.dumps(review, separators=(",", ":"))
    # "<" only occurs inside JSON strings, so escaping it keeps the data from
    # closing the <script> tag or opening an HTML comment, and stays valid JSON
    data = data.replace("<", "\\u003c")
    return template.replace(PLACEHOLDER, data, 1)


def write(review: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "review.json"
    html_path = out_dir / "report.html"
    review["meta"]["review_path"] = str(json_path)
    json_path.write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(render_html(review), encoding="utf-8")
    return json_path, html_path


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def schema() -> dict:
    return json.loads(resources.files("pr_review").joinpath("schema/review.schema.json").read_text(encoding="utf-8"))
