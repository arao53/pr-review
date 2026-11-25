"""pr-review: triage, cluster and map large Python pull requests."""

__version__ = "0.3.0"
SCHEMA_VERSION = "1"

from pr_review.analyze import analyze  # noqa: E402

__all__ = ["analyze", "__version__", "SCHEMA_VERSION"]
