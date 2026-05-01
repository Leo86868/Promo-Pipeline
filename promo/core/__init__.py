"""Promo core package — shared utilities."""

import re


_MATERIAL_SLUG_SEP_RE = re.compile(r"[-_]+")


def sanitize_poi_name(name: str) -> str:
    """Sanitize a POI name for safe use in file paths.

    Canonical implementation used by all promo modules. Handles:
    - Path separators (/, \\) → underscore
    - Null bytes → removed
    - Leading dots → stripped (prevents hidden files / traversal)
    - Spaces → underscores, lowercased
    - Non-alphanumeric chars (except underscore, hyphen) → removed
    """
    # Remove path separators and null bytes
    safe = name.replace("/", "_").replace("\\", "_").replace("\0", "")
    # Collapse leading dots to prevent hidden files / traversal
    safe = safe.lstrip(".")
    # Replace spaces with underscores and lowercase
    safe = safe.replace(" ", "_").lower()
    # Remove any remaining chars that aren't alphanumeric, underscore, or hyphen
    safe = re.sub(r"[^\w\-]", "", safe)
    return safe or "unnamed"


def material_poi_slug(name: str) -> str:
    """Canonical material-directory slug for a display-name POI.

    Material pools use hyphenated directory names (`material/<slug>/...`),
    while sidecars and staging paths still route through `sanitize_poi_name()`
    and therefore keep underscore-style slugs.
    """
    safe = sanitize_poi_name(name)
    slug = _MATERIAL_SLUG_SEP_RE.sub("-", safe).strip("-")
    return slug or "unnamed"
