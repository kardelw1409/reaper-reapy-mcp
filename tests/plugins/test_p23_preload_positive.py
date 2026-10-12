"""P23 tests, workstream B: a compatible v2 profile is really preloaded and reused.

The uncertainty this file answers. P22 measured the startup preload reporting
*13 entries skipped as superseded and zero parsed*, which proves obsolete entries
are rejected - but **not** that a compatible schema-v2 entry is loaded into memory
and served. A start with zero compatible profiles is not evidence that successful
preload works, so the positive case is demonstrated here explicitly, through the
real lifecycle and with counters that can tell the two apart:

1. build one entry through the **real** resolve path, with the **real** Vital
   provider reading the pinned fixture registry, into an isolated cache directory;
2. start the server through `server.create_server()` - only the host *controller* is
   stubbed (starting the real one would connect to REAPER); the preload itself is the
   production one, reading the isolated directory from the environment;
3. a subsequent warm call must report `prepared`, build nothing and read **no** cache
   file - asserted with a counter on `Path.read_text` inside the cache directory;
4. a tool-layer `plugin_read` must still take its values from the host, live
   (a value changed between two calls is visible), while the profile comes from memory.

The negative cases are the ones that must not become a reason for the positive one to
fail: a schema-v1 entry and a stale v2 entry (another provider revision / another
source digest) sit in the same directory, are reported with their own reasons, and the
valid entry is still loaded. With no valid entry at all, the preload prepares nothing
and the warm call falls back to the build - the existing safe behaviour.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import mock_host as MH

from reaper_reapy_mcp.plugins import api as A
from reaper_reapy_mcp.plugins import cache as C
from reaper_reapy_mcp.plugins import preload as PL
from reaper_reapy_mcp.plugins import profile as P
from reaper_reapy_mcp.plugins.providers.vital import profile_build as PB
from reaper_reapy_mcp.plugins.providers.vital.provider import VitalProvider
from reaper_reapy_mcp.plugins.resolve import resolve_instance_profile

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REGISTRY_DIR = FIXTURES / "vital_registry"
NOT_THERE = FIXTURES / "no_calibration_configured.json"

IDENTITY = {
    "fx_name": "VST3i: Vital (Vital Audio)",
    "class_id": "56535456697461766974616C00000000",
    "module": "Vital.vst3",
    "param_count": 8,
}

#: The names the mock host reports at its 8 indices: the fixture registry's own
#: names, so the read path's index-vs-name check is the real one (P8).
HOST_PARAM_NAMES = {
    0: "Beats Per Minute",
    1: "Chorus Filter Cutoff",
    2: "Chorus Delay 1",
    3: "Chorus Delay 2",
    4: "Chorus Mix",
    5: "Chorus Feedback",
    6: "(no control at index 6)",
    7: "(no control at index 7)",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def vital_host(*, values=None) -> MH.MockHostBridge:
    slot = MH.MockFxSlot(
        name=IDENTITY["fx_name"],
        module=IDENTITY["module"],
        class_id=IDENTITY["class_id"],
        params=dict(values or {str(1000 + index): 0.25 for index in range(8)}),
        param_names=dict(HOST_PARAM_NAMES),
    )
    return MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", [slot])])


def isolated(monkeypatch, cache_dir: Path) -> None:
    """Point the production environment at the fixture registry and a private cache."""
    monkeypatch.setenv("REAPER_MCP_PLUGIN_PROVIDERS", "vital")
    monkeypatch.setenv("REAPER_MCP_VITAL_REGISTRY_DIR", str(REGISTRY_DIR))
    monkeypatch.setenv("REAPER_MCP_PLUGIN_CACHE", str(cache_dir))
    monkeypatch.setenv("REAPER_MCP_VITAL_CALIBRATION", str(NOT_THERE))
    monkeypatch.setenv("REAPER_MCP_VITAL_ENUM_BOUNDARIES", str(NOT_THERE))


def real_provider() -> VitalProvider:
    return VitalProvider(registry=PB.load_registry(directory=str(REGISTRY_DIR)))


def cold_build(cache_dir: Path):
    """Build and save one entry through the real resolve path."""
    host = vital_host()
    provider = real_provider()
    cache = C.ProfileCache(provider_id="vital", directory=cache_dir)
    resolution = resolve_instance_profile(host, provider, cache=cache)
    return host, resolution


def start_server(monkeypatch):
    """`create_server` with only the host controller stubbed (the preload is real)."""
    from reaper_reapy_mcp import server as S

    monkeypatch.setattr(S, "ReaperController", lambda debug=True: object())
    monkeypatch.setattr(S, "setup_mcp_tools", lambda mcp, controller: None)
    monkeypatch.setattr(S, "setup_plugin_tools", lambda mcp: None)
    return S.create_server(debug=False)


class ReadCounter:
    """Counts `Path.read_text` calls under one directory, delegating everything."""

    def __init__(self, monkeypatch, directory: Path) -> None:
        self.directory = Path(directory)
        self.count = 0
        original = Path.read_text

        def counting(self_path: Path, *args, **kwargs):
            try:
                if Path(self_path).parent == self.directory:
                    counter.count += 1
            except OSError:  # pragma: no cover - defensive only
                pass
            return original(self_path, *args, **kwargs)

        counter = self
        monkeypatch.setattr(Path, "read_text", counting)


def write_variant(path: Path, key: str, entry: dict) -> Path:
    variant = path / f"profile_{key[:24]}.json"
    variant.write_text(json.dumps(entry), encoding="utf-8")
    return variant


# ---------------------------------------------------------------------------
# 1. The positive case, end to end through create_server
# ---------------------------------------------------------------------------


def test_a_compatible_v2_profile_is_loaded_at_startup_and_served_without_a_rebuild(
    tmp_path: Path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    isolated(monkeypatch, cache_dir)
    _host, cold = cold_build(cache_dir)
    key = cold.cache.key
    entry_path = cache_dir / f"profile_{key[:24]}.json"
    assert entry_path.exists(), "the cold path must have written one entry"
    assert cold.profile["profile_schema_version"] == P.PROFILE_SCHEMA_VERSION == 2
    assert P.validate_profile(cold.profile).ok, P.validate_profile(cold.profile).codes()

    PL.prepared_profiles().clear()
    started = time.perf_counter()
    server = start_server(monkeypatch)
    startup_seconds = time.perf_counter() - started
    assert server is not None
    report = PL.prepared_profiles().report
    assert report is not None, "create_server must have run the preload"

    # --- the positive evidence: the compatible entry was *parsed and loaded* ---
    assert report.ran is True
    assert report.files_scanned == 1
    assert report.n_prepared == 1, report.as_dict()
    assert report.prepared[0]["key"] == key
    assert report.n_skipped == 0
    assert len(PL.prepared_profiles()) == 1
    prepared = PL.prepared_lookup(cache_dir, key)
    assert prepared is not None and prepared.profile["profile_schema_version"] == 2

    # --- the warm call: from memory, no build, no cache-file read ---
    counter = ReadCounter(monkeypatch, cache_dir)
    builds = {"n": 0}

    class Counting(VitalProvider):
        def build_profile(self, identity):  # type: ignore[override]
            builds["n"] += 1
            return super().build_profile(identity)

    warm = Counting(registry=PB.load_registry(directory=str(REGISTRY_DIR)))
    cache = C.ProfileCache(provider_id="vital", directory=cache_dir)
    warm_started = time.perf_counter()
    resolution = resolve_instance_profile(vital_host(), warm, cache=cache)
    warm_seconds = time.perf_counter() - warm_started

    assert resolution.cache.source == "cache"
    assert resolution.cache.key == key
    assert cache.status.prepared is True, "the startup index must answer this call"
    assert cache.status.built is False, "a prepared profile must not be rebuilt"
    assert cache.status.key_without_build is True, "the key came from the declaration"
    assert counter.count == 0, (
        "the entry was read and validated once at startup; the warm call must not read it again"
    )
    assert C.profile_content_hash(resolution.profile) == C.profile_content_hash(cold.profile)
    assert builds["n"] == 0, "the provider counted its own builds: a prepared hit performs none"
    assert startup_seconds < 5.0 and warm_seconds < 5.0, (startup_seconds, warm_seconds)


def test_the_warm_tool_call_reads_its_values_from_the_host_not_from_the_profile(
    tmp_path: Path, monkeypatch
):
    """The profile is cached; the *values* never are."""
    cache_dir = tmp_path / "cache"
    isolated(monkeypatch, cache_dir)
    _host, cold = cold_build(cache_dir)
    key = cold.cache.key
    PL.prepared_profiles().clear()
    start_server(monkeypatch)
    assert PL.prepared_profiles().report.n_prepared == 1, "the positive case is required here"

    host = vital_host(values={str(1000 + index): 0.25 for index in range(8)})
    monkeypatch.setattr(A, "_BRIDGE", host)
    monkeypatch.setattr(A, "_REGISTRY", None)
    config = A.PluginConfig(provider_ids=("vital",), cache_dir=str(cache_dir), env={})

    first = A.plugin_read(
        config=config, instance={"track_index": 0, "fx_index": 0}, controls=["beats_per_minute"]
    )
    assert first["ok"] is True, first
    assert first["meta"]["cache"]["source"] == "cache"
    assert first["meta"]["cache"]["prepared"] is True
    assert first["meta"]["cache"]["built"] is False
    normalised = first["data"]["values"]["beats_per_minute"]["normalized"]
    assert normalised == pytest.approx(0.25)
    reads_after_first = host.count("read_params")

    # the host's value moves; the very next call must see the new one
    slot = host.tracks[0].slots[0]
    slot.params[slot.ident_by_index[0]] = 0.75
    second = A.plugin_read(
        config=config, instance={"track_index": 0, "fx_index": 0}, controls=["beats_per_minute"]
    )
    assert second["data"]["values"]["beats_per_minute"]["normalized"] == pytest.approx(0.75)
    assert host.count("read_params") > reads_after_first, (
        "a value read must be a host read on every call: the profile caches capability, "
        "never state"
    )
    assert key is not None


# ---------------------------------------------------------------------------
# 2. The negative cases: they must not stop the valid entry from loading
# ---------------------------------------------------------------------------


def test_a_schema_v1_entry_and_a_stale_v2_entry_are_reported_while_the_valid_one_loads(
    tmp_path: Path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    isolated(monkeypatch, cache_dir)
    _host, cold = cold_build(cache_dir)
    key = cold.cache.key
    valid = cache_dir / f"profile_{key[:24]}.json"

    raw = json.loads(valid.read_text(encoding="utf-8"))
    v1 = json.loads(json.dumps(raw))
    v1["profile_schema_version"] = 1
    v1["profile"]["profile_schema_version"] = 1
    v1["cache_format_key"] = "capability-map-v1"
    v1_path = write_variant(cache_dir, "1" * 64, v1)

    stale = json.loads(json.dumps(raw))
    stale["profile"]["provenance"]["source_digest"] = "0" * 64
    stale["cache_key"] = "2" * 64
    stale_path = write_variant(cache_dir, "2" * 64, stale)

    assert valid.exists() and v1_path.exists() and stale_path.exists()
    PL.prepared_profiles().clear()
    start_server(monkeypatch)
    report = PL.prepared_profiles().report

    assert report.n_prepared == 1, report.as_dict()
    assert report.prepared[0]["key"] == key, "the *valid* entry is the one loaded"
    rules = {row.get("file"): row.get("rule") for row in report.skipped}
    assert rules.get(v1_path.name) == "preload.format_key", rules
    assert rules.get(stale_path.name) == "preload.superseded", rules
    assert report.files_scanned == 3
    assert len(PL.prepared_profiles()) == 1
    # nothing was deleted, quarantined or rewritten by starting up
    assert {path.name for path in cache_dir.glob("profile_*.json")} == {
        valid.name,
        v1_path.name,
        stale_path.name,
    }
    assert json.loads(v1_path.read_text(encoding="utf-8"))["profile_schema_version"] == 1


def test_with_no_valid_entry_the_preload_prepares_nothing_and_the_call_builds(
    tmp_path: Path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    isolated(monkeypatch, cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    PL.prepared_profiles().clear()
    start_server(monkeypatch)
    report = PL.prepared_profiles().report
    assert report.n_prepared == 0
    assert len(PL.prepared_profiles()) == 0

    marker = {"built": 0}

    class Counting(VitalProvider):
        def build_profile(self, identity):  # type: ignore[override]
            marker["built"] += 1
            return super().build_profile(identity)

    provider = Counting(registry=PB.load_registry(directory=str(REGISTRY_DIR)))
    cache = C.ProfileCache(provider_id="vital", directory=cache_dir)
    resolution = resolve_instance_profile(vital_host(), provider, cache=cache)
    assert resolution.cache.source == "built"
    assert marker["built"] == 1, "with nothing prepared the call builds, as before"
    assert cache.status.prepared is False
    assert (cache_dir / f"profile_{resolution.cache.key[:24]}.json").exists()


def test_a_prepared_entry_whose_file_changed_underneath_is_not_served(tmp_path: Path, monkeypatch):
    """The stat guard: a prepared copy is only served while its file is the same one."""
    cache_dir = tmp_path / "cache"
    isolated(monkeypatch, cache_dir)
    _host, cold = cold_build(cache_dir)
    key = cold.cache.key
    PL.prepared_profiles().clear()
    start_server(monkeypatch)
    assert PL.prepared_profiles().report.n_prepared == 1

    path = cache_dir / f"profile_{key[:24]}.json"
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["written_by"] = "something else entirely"
    path.write_text(json.dumps(entry), encoding="utf-8")

    counter = ReadCounter(monkeypatch, cache_dir)
    cache = C.ProfileCache(provider_id="vital", directory=cache_dir)
    resolution = resolve_instance_profile(vital_host(), real_provider(), cache=cache)
    assert cache.status.prepared is False, "a changed file must fall through to the disk path"
    assert counter.count >= 1
    assert resolution.profile is not None, "and the disk path still serves a usable profile"


def test_the_preload_reports_its_reasons_and_never_raises_for_a_broken_directory(tmp_path, monkeypatch):
    missing = tmp_path / "absent"
    isolated(monkeypatch, missing)
    PL.prepared_profiles().clear()
    report = PL.preload_from_env()
    assert report.ran is False
    assert report.reason == "cache_directory_absent"
    assert len(PL.prepared_profiles()) == 0
