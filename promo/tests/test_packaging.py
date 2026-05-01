"""Packaging metadata + wheel-content regression guards.

These tests protect the supported install contract (unpacked checkout /
handoff artifact via ``pip install -e .``) while also smoke-checking the
generated wheel for Python-package drift. The wheel is NOT a supported
runtime artifact because the Remotion workspace remains repo-relative, but
it is still the cheapest way to catch missing runtime packages and bundled
persona assets before they disappear from the tree again.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _pyproject_data() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _wheel_path(tmp_path: Path) -> Path:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--no-build-isolation",
            "-w",
            str(tmp_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (
        "pip wheel failed\n"
        f"stdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )
    wheels = sorted(tmp_path.glob("pgc_pipeline-*.whl"))
    assert len(wheels) == 1, f"Expected 1 wheel, found {wheels}"
    return wheels[0]


def test_setuptools_uses_promo_package_discovery() -> None:
    data = _pyproject_data()
    include = data["tool"]["setuptools"]["packages"]["find"]["include"]
    assert include == ["promo*"], (
        "Package discovery must cover the full promo tree so newly-added "
        "runtime subpackages do not have to be hand-added to a stale list."
    )


def test_arsenal_yaml_md_declared_as_package_data() -> None:
    """Sprint Arsenal Externalization (Commit 7): persona / system
    prompt / voice / script-skeleton libraries each ship as separate
    sub-package data globs. ``find_packages(include=["promo*"])`` only
    discovers sub-packages with an ``__init__.py``, and only data files
    matching the globs below ride along into the wheel."""
    data = _pyproject_data()
    package_data = data["tool"]["setuptools"]["package-data"]
    expected = {
        "promo.arsenal.personas": "*.yaml",
        "promo.arsenal.system_prompts": "*.md",
        "promo.arsenal.voices": "*.yaml",
        "promo.arsenal.script_skeletons": "*.yaml",
    }
    for key, glob in expected.items():
        assert key in package_data, f"package-data must declare {key!r}"
        assert glob in package_data[key], (
            f"{key} package-data must include {glob!r}; "
            f"got {package_data[key]!r}"
        )


def test_built_wheel_contains_runtime_packages_and_arsenal_data(tmp_path: Path) -> None:
    """AC-24: the built wheel ships every arsenal data file. Extends
    the prior persona-only check to cover all 4 arsenal sub-libraries."""
    wheel_path = _wheel_path(tmp_path)
    with zipfile.ZipFile(wheel_path) as zf:
        names = set(zf.namelist())

    required = {
        "promo/cli/compile_promo.py",
        "promo/core/pipeline/__init__.py",
        "promo/core/selection/__init__.py",
        "promo/core/selection/format_selectors.py",
        "promo/core/selection/persona_selectors.py",
        # Sprint Arsenal Externalization (Commit 7) — relocated from
        # promo/personas/ + new arsenal sub-libraries:
        "promo/arsenal/__init__.py",
        "promo/arsenal/personas/__init__.py",
        "promo/arsenal/personas/_loader.py",
        "promo/arsenal/personas/third_person_promo.yaml",
        "promo/arsenal/system_prompts/__init__.py",
        "promo/arsenal/system_prompts/mimo_clip_analysis_v1.md",
        "promo/arsenal/system_prompts/gemini1_script_v1.md",
        "promo/arsenal/system_prompts/gemini1_f3_retry_v1.md",
        "promo/arsenal/system_prompts/gemini2_assign_v1.md",
        "promo/arsenal/voices/__init__.py",
        "promo/arsenal/voices/catalog.yaml",
        "promo/arsenal/script_skeletons/__init__.py",
        "promo/arsenal/script_skeletons/short_30s.yaml",
        "promo/arsenal/script_skeletons/long_65s.yaml",
    }
    missing = sorted(required - names)
    assert not missing, (
        "Wheel is missing runtime Python packages / arsenal assets required "
        f"by the current compile path: {missing}"
    )
