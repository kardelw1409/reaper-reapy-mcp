"""P0 boundary tests (14 P0 test list; 08 §8; AUD-013).

The L0 boundary is a STATIC scan of `plugins/` - the parent package's
pre-existing import chain imports host modules, so a runtime module check would
be vacuous (documented in IMPLEMENTATION-STATUS.md, P0).
"""

from __future__ import annotations

from pathlib import Path

from l0_boundary import (
    ALLOWED_HOST_IMPORTERS,
    BANNED_PROVIDER_IDENTIFIERS,
    check_boundaries,
    plugin_python_files,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_ROOT = REPO_ROOT / "src" / "reaper_reapy_mcp" / "plugins"

EXPECTED_CORE_MODULES = (
    "__init__.py",
    "errors.py",
    "model.py",
    "profile.py",
    "provider.py",
    "tools.py",
    "host/__init__.py",
    "host/contract.py",
)


def test_expected_core_modules_exist():
    """The scan list is explicit and non-empty so the boundary test cannot pass
    vacuously (14 P0 work item 7)."""
    rels = {path.relative_to(PLUGINS_ROOT).as_posix() for path in plugin_python_files(PLUGINS_ROOT)}
    missing = [name for name in EXPECTED_CORE_MODULES if name not in rels]
    assert not missing, f"missing core modules: {missing}"


def test_l0_boundary_is_clean():
    files, violations = check_boundaries(PLUGINS_ROOT)
    assert files, "the boundary scan must examine at least one file"
    assert violations == [], violations


def test_l0_checker_detects_host_import_probe(tmp_path: Path):
    """P0-2: with a probe file that imports the host API in core, the test
    fails. A boundary test that cannot fail on a violation is not one."""
    (tmp_path / "core.py").write_text("import reapy\n", encoding="utf-8")
    files, violations = check_boundaries(tmp_path)
    assert len(files) == 1
    assert violations and "core.py" in violations[0]


def test_l0_checker_detects_lazy_host_import_probe(tmp_path: Path):
    (tmp_path / "core.py").write_text(
        "def f():\n    from reapy import reascript_api\n    return reascript_api\n",
        encoding="utf-8",
    )
    _, violations = check_boundaries(tmp_path)
    assert violations, "a function-local host import is still a violation"


def test_l0_checker_detects_provider_identifier_probe(tmp_path: Path):
    identifier = BANNED_PROVIDER_IDENTIFIERS[0]
    (tmp_path / "core.py").write_text(f"# mentions {identifier} in a comment\n", encoding="utf-8")
    _, violations = check_boundaries(tmp_path)
    assert violations and "provider identifier" in violations[0]


def test_l0_checker_allows_the_declared_host_importer(tmp_path: Path):
    target = tmp_path / "host" / "reaper.py"
    target.parent.mkdir(parents=True)
    target.write_text("import reapy\n", encoding="utf-8")
    _, violations = check_boundaries(tmp_path, allowed_host_importers=ALLOWED_HOST_IMPORTERS)
    assert violations == []


def test_l0_checker_allows_provider_identifiers_only_in_providers_package(tmp_path: Path):
    provider_file = tmp_path / "providers" / "demo" / "codec.py"
    provider_file.parent.mkdir(parents=True)
    provider_file.write_text(
        f"NAME = '{BANNED_PROVIDER_IDENTIFIERS[0]}'\n", encoding="utf-8"
    )
    _, violations = check_boundaries(tmp_path, banned_identifiers=BANNED_PROVIDER_IDENTIFIERS)
    assert violations == []
