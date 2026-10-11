"""P21 tests: the prepared-profile preload and the warm path that builds nothing.

Two properties are pinned here, and they are different claims:

1. **A warm cache hit performs no profile build.** Before this round
   `resolve_instance_profile` projected the whole profile *before* consulting the
   cache and threw the projection away on a hit (measured live 2026-10-11:
   ~600 ms of pure compute per call on the 775-control Vital profile). The fast
   path computes the cache key from the provider's declared material
   (`profile_source_rows`) and only builds on a miss - and the build, when it
   happens, must re-derive the very key the declaration produced (a disagreement
   is a loud `cache.key_material` refusal, never a mis-filed entry).
2. **A prepared profile is loaded at startup and served without research.** The
   preload validates every identity-independent property of a cache entry once,
   holds the entries in memory, never writes/quarantines/deletes, and a served
   prepared profile is never mutated by a call.

Everything runs offline against the mock provider and a mock host; no test
touches the real cache directory or REAPER.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import fixtures as F
import mock_host as MH
from mock_provider import MockProvider

from reaper_reapy_mcp.plugins import binding as B
from reaper_reapy_mcp.plugins import cache as C
from reaper_reapy_mcp.plugins import preload as PL
from reaper_reapy_mcp.plugins import resolve as RS
from reaper_reapy_mcp.plugins.errors import IncompatibleSettings
from reaper_reapy_mcp.plugins.providers import registry as provider_registry
from reaper_reapy_mcp.plugins.resolve import resolve_instance_profile, resolve_instances

#: The rows `fixtures.demo_profile()` declares in `provenance.sources`.
DEMO_SOURCE_ROWS = [{"file": "demo_registry.json", "sha256": "demo-source-digest"}]


class FastProvider(MockProvider):
    """A provider that declares its key material without a build (P21 hook)."""

    def __init__(self, *, rows=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.builds = 0
        self.adopted: dict | None = None
        self._rows = DEMO_SOURCE_ROWS if rows is None else rows

    def build_profile(self, identity):
        self.builds += 1
        return super().build_profile(identity)

    def profile_source_rows(self, identity=None):
        return [dict(row) for row in self._rows]

    def adopt_profile(self, profile):
        self.adopted = dict(profile)


class BrokenMaterialProvider(FastProvider):
    """A provider whose declared key material cannot be produced at all."""

    def profile_source_rows(self, identity=None):
        raise IncompatibleSettings(
            "the registry directory is unusable (test)", detail={"reason": "test"}
        )


def env_for(directory: Path) -> dict[str, str]:
    return {C.CACHE_DIR_ENV: str(directory), "REAPER_MCP_PLUGIN_PROVIDERS": F.DEMO_PROVIDER}


def make_cache(directory: Path, provider: MockProvider) -> C.ProfileCache:
    return C.ProfileCache(provider_id=str(provider.id), directory=directory)


def use_provider(monkeypatch, provider) -> None:
    """Make the preload's provider lookup answer this mock (no real provider)."""
    monkeypatch.setattr(
        provider_registry, "load_provider", lambda provider_id, **kwargs: provider
    )


# ---------------------------------------------------------------------------
# 1. The warm path
# ---------------------------------------------------------------------------


def test_a_warm_hit_builds_no_profile_and_still_serves_the_profile(tmp_path: Path):
    host = MH.MockHostBridge()
    directory = tmp_path / "cache"
    cold = FastProvider()
    first = resolve_instance_profile(
        host, cold, cache=make_cache(directory, cold), registry=None
    )
    assert cold.builds == 1
    assert first.cache.source == "built"
    assert first.cache.built is True

    warm = FastProvider()
    second = resolve_instance_profile(host, warm, cache=make_cache(directory, warm))
    assert warm.builds == 0, (
        "a warm cache hit must not project the profile it is about to discard "
        "(the defect this round fixes: the build ran before the cache was consulted)"
    )
    assert second.cache.source == "cache"
    assert second.cache.built is False
    assert second.cache.key_without_build is True
    assert C.profile_content_hash(second.profile) == C.profile_content_hash(first.profile)
    assert second.usable_for_writes is True


def test_the_declared_material_and_the_built_provenance_derive_the_same_key(tmp_path: Path):
    provider = FastProvider()
    facts = MH.MockFxSlot().facts(track_index=0, fx_index=0, track_name="Track 1", fx_count=1)
    identity = provider.identify(facts)
    material = C.key_material_of(provider, identity)
    assert material is not None
    build = C.build_capability_profile(provider, identity)
    assert material.key_for(F.DEMO_PROVIDER, identity) == build.key_for(
        F.DEMO_PROVIDER, identity
    ), "the declaration and the built provenance must derive one key (the runtime cross-check)"


def test_a_provider_without_the_hook_keeps_the_build_then_load_order(tmp_path: Path):
    """No hook means the established order - correct, and still not a rebuild per call."""
    host = MH.MockHostBridge()
    directory = tmp_path / "cache"
    cold = MockProvider()
    resolve_instance_profile(host, cold, cache=make_cache(directory, cold))

    plain = MockProvider()
    assert C.key_material_of(plain, {}) is None
    second = resolve_instance_profile(host, plain, cache=make_cache(directory, plain))
    assert second.cache.source == "cache", "the cache is still consulted"
    assert second.cache.key_without_build is False
    assert second.cache.built is True, (
        "a provider without the hook pays the build on a hit; the status must say so "
        "rather than pretend the warm path was used"
    )


def test_a_disagreeing_declaration_is_refused_and_nothing_is_cached(tmp_path: Path):
    host = MH.MockHostBridge()
    directory = tmp_path / "cache"
    provider = FastProvider(rows=[{"file": "demo_registry.json", "sha256": "other-digest"}])
    resolution = resolve_instance_profile(host, provider, cache=make_cache(directory, provider))

    # the profile is still served (the build is authoritative for content) ...
    assert resolution.profile.get("controls")
    assert resolution.usable_for_writes is True
    rules = [refusal.as_row()["rule"] for refusal in resolution.cache.refusals]
    assert "cache.key_material" in rules, resolution.cache.as_dict()
    assert resolution.cache.warnings, "the disagreement must be loud, not silent"
    # ... but nothing may be filed under a key its provenance does not re-derive
    files = list(directory.glob("profile_*.json")) if directory.exists() else []
    assert files == [], (
        "a profile whose provenance disagrees with the declared key material must not "
        f"be written at all (found {[path.name for path in files]})"
    )


def test_a_cache_hit_adopts_the_served_profile_into_the_provider(tmp_path: Path):
    host = MH.MockHostBridge()
    directory = tmp_path / "cache"
    cold = FastProvider()
    resolve_instance_profile(host, cold, cache=make_cache(directory, cold))

    warm = FastProvider()
    resolution = resolve_instance_profile(host, warm, cache=make_cache(directory, warm))
    assert warm.builds == 0
    assert warm.adopted is not None, (
        "the provider must receive the profile that was served: a later "
        "`dependencies()`/`disable_targets()` call reads its own state, and an empty "
        "profile there would silently weaken rule evaluation"
    )
    assert C.profile_content_hash(warm.adopted) == C.profile_content_hash(resolution.profile)


def test_a_location_selector_reads_only_the_addressed_tracks_slots(tmp_path: Path):
    host = MH.MockHostBridge(
        tracks=[
            MH.MockTrack(0, "Track 1", [MH.MockFxSlot(), MH.MockFxSlot()]),
            MH.MockTrack(1, "Track 2", [MH.MockFxSlot(), MH.MockFxSlot()]),
        ]
    )
    provider = MockProvider()
    resolved = resolve_instance_profile(
        host, provider, selector={"track_index": 1, "fx_index": 0}, cache=make_cache(tmp_path / "c", provider)
    )
    assert resolved.instance.ref.location == {"track_index": 1, "fx_index": 0}
    assert host.count("fx_facts") == 1, (
        "an addressed track must not have every other track's slots read: the facts of "
        "the addressed slot are still read (identity is verified, never assumed), but "
        f"the sweep stops there (measured live: 1536 ms -> 687 ms for 4 slots)"
    )

    host.reset_log()
    provider2 = MockProvider()
    listed = RS.resolve_instances(host, provider2)
    assert len(listed) == 4, "an unfiltered listing still sees every slot"
    assert host.count("fx_facts") == 4, (
        "a listing that names no selector must still enumerate every slot - the "
        "narrowing must not become a guess"
    )


# ---------------------------------------------------------------------------
# 2. The startup preload
# ---------------------------------------------------------------------------


class ReadCounter:
    """Counts `Path.read_text` calls under one directory, delegating everything."""

    def __init__(self, monkeypatch, directory: Path) -> None:
        self.directory = directory
        self.count = 0
        original = Path.read_text

        def counting(self_path: Path, *args, **kwargs):
            try:
                if Path(self_path).parent == directory:
                    counter.count += 1
            except OSError:  # pragma: no cover - defensive only
                pass
            return original(self_path, *args, **kwargs)

        counter = self
        monkeypatch.setattr(Path, "read_text", counting)


def prepare(tmp_path: Path, monkeypatch, provider=None, *, identity=None):
    """Build one entry through the real resolve path, then return its pieces."""
    host = MH.MockHostBridge()
    directory = tmp_path / "cache"
    provider = provider or FastProvider()
    resolution = resolve_instance_profile(host, provider, cache=make_cache(directory, provider))
    key = resolution.cache.key
    assert key, resolution.cache.as_dict()
    assert (directory / f"profile_{key[:24]}.json").exists(), "the cold path must have written"
    env = env_for(directory)
    monkeypatch.setattr(
        provider_registry, "load_provider", lambda provider_id, **kwargs: provider
    )
    return host, directory, env, key, resolution


def test_the_preload_holds_at_most_the_cap_and_reports_the_rest(tmp_path: Path, monkeypatch):
    """A bounded startup: the cap limits what is held, and nothing is deleted."""
    directory = tmp_path / "cache"
    base_identity = {
        "fx_name": "VST3i: Demo Synth",
        "class_id": F.DEMO_CLASS,
        "module": F.DEMO_MODULE,
        "param_count": 8,
    }
    build = C.build_capability_profile(FastProvider(), base_identity)
    cache = C.ProfileCache(provider_id=F.DEMO_PROVIDER, directory=directory)
    for count in (8, 9, 10):
        cache.save(build.key_for(F.DEMO_PROVIDER, dict(base_identity, param_count=count)), build.profile)
    assert len(list(directory.glob("profile_*.json"))) == 3

    monkeypatch.setattr(PL, "PRELOAD_ENTRY_CAP_PER_PROVIDER", 1)
    use_provider(monkeypatch, FastProvider())
    report = PL.preload_from_env(env_for(directory))

    assert report.n_prepared == 1, "the cap bounds what is held in memory"
    assert report.capped == 2, "the entries beyond the cap are reported, not silently dropped"
    assert report.files_scanned + report.capped == 3
    assert len(list(directory.glob("profile_*.json"))) == 3, (
        "a cap is a bound on the preload, never a deletion: the runtime disk path still "
        "serves an entry that was not prepared"
    )


def test_preload_prepares_the_current_entry_and_serves_it_without_reading_the_file(
    tmp_path: Path, monkeypatch
):
    _host, directory, env, key, cold = prepare(tmp_path, monkeypatch)

    report = PL.preload_from_env(env)
    assert report.ran is True
    assert report.n_prepared == 1, report.as_dict()
    assert report.prepared[0]["key"] == key
    assert report.files_scanned == 1
    assert report.seconds >= 0.0

    counter = ReadCounter(monkeypatch, directory)
    provider = FastProvider()
    cache = C.ProfileCache(provider_id=F.DEMO_PROVIDER, directory=directory)
    cache.begin_call()
    loaded = cache.load(key, identity=cold.identity)
    assert loaded.status == "hit" and loaded.profile is not None
    assert cache.status.prepared is True, "the entry validated at startup must answer the call"
    assert counter.count == 0, (
        "a prepared entry was already read and validated at startup; the call must not "
        "read it again"
    )

    counter.count = 0
    PL.prepared_profiles().clear()
    cache2 = C.ProfileCache(provider_id=F.DEMO_PROVIDER, directory=directory)
    cache2.begin_call()
    disk = cache2.load(key, identity=cold.identity)
    assert disk.status == "hit" and disk.profile is not None
    assert cache2.status.prepared is False
    assert counter.count == 1, "without the index the entry is read from disk"
    assert C.profile_content_hash(disk.profile) == C.profile_content_hash(loaded.profile)


def test_preload_never_writes_quarantines_or_deletes(tmp_path: Path, monkeypatch):
    _host, directory, env, key, _cold = prepare(tmp_path, monkeypatch)
    target = directory / f"profile_{key[:24]}.json"
    corrupt = directory / "profile_ffffffffffffffffffffffff.json"
    corrupt.write_text("{ not json", encoding="utf-8")
    stale = directory / "profile_000000000000000000000000.json"
    stale.write_text(json.dumps({"cache_format_key": "old", "profile_schema_version": 0}), encoding="utf-8")
    before = {path.name: path.read_bytes() for path in sorted(directory.glob("*"))}

    report = PL.preload_from_env(env)

    after = {path.name: path.read_bytes() for path in sorted(directory.glob("*"))}
    assert before == after, (
        "startup must not mutate user data as a side effect of starting: whatever it "
        "skips stays exactly where it was, for the runtime path to report or quarantine"
    )
    rows = {row.get("file"): row.get("rule") for row in report.skipped}
    assert rows.get(corrupt.name) == "preload.unparsable", rows
    assert rows.get(stale.name) == "preload.format_key", rows
    assert "preload" in rows.get(target.name, "preload"), rows
    assert report.n_prepared + report.n_skipped == report.files_scanned == 3


def test_a_file_changed_since_startup_is_not_served_from_the_index(tmp_path: Path, monkeypatch):
    _host, directory, env, key, cold = prepare(tmp_path, monkeypatch)
    assert PL.preload_from_env(env).n_prepared == 1
    target = directory / f"profile_{key[:24]}.json"
    entry = json.loads(target.read_text(encoding="utf-8"))
    entry["profile"]["limits"] = ["changed after startup"]
    target.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    cache = C.ProfileCache(provider_id=F.DEMO_PROVIDER, directory=directory)
    cache.begin_call()
    loaded = cache.load(key, identity=cold.identity)
    assert cache.status.prepared is False, (
        "the prepared copy describes bytes that are no longer there; the disk path - "
        "which reports and quarantines - must take over"
    )
    assert loaded.status in ("hit", "refused")


def test_a_disagreeing_identity_is_never_served_from_the_index(tmp_path: Path, monkeypatch):
    _host, directory, env, key, cold = prepare(tmp_path, monkeypatch)
    assert PL.preload_from_env(env).n_prepared == 1
    wrong = dict(cold.identity)
    wrong["param_count"] = int(wrong["param_count"]) + 1

    cache = C.ProfileCache(provider_id=F.DEMO_PROVIDER, directory=directory)
    cache.begin_call()
    cache.load(key, identity=wrong)
    assert cache.status.prepared is False, (
        "a prepared entry answers a call only when the live identity agrees with it, "
        "exactly like the disk path"
    )
    assert [refusal.rule for refusal in cache.status.refusals]


def test_preload_with_no_provider_configured_is_a_no_op(tmp_path: Path, monkeypatch):
    counter = ReadCounter(monkeypatch, tmp_path)
    report = PL.preload_from_env({C.CACHE_DIR_ENV: str(tmp_path)})
    assert report.ran is False
    assert report.reason == "no_provider_configured"
    assert report.files_scanned == 0
    assert counter.count == 0, "a workspace without providers must pay nothing at startup"


def test_a_broken_provider_becomes_a_report_row_and_the_other_one_still_loads(
    tmp_path: Path, monkeypatch
):
    host = MH.MockHostBridge()
    directory = tmp_path / "cache"
    good = FastProvider()
    resolution = resolve_instance_profile(host, good, cache=make_cache(directory, good))
    key = resolution.cache.key

    broken = BrokenMaterialProvider()
    providers = {F.DEMO_PROVIDER: good, "broken": broken}
    monkeypatch.setattr(
        provider_registry, "load_provider", lambda provider_id, **kwargs: providers[provider_id]
    )
    env = dict(env_for(directory), REAPER_MCP_PLUGIN_PROVIDERS=f"{F.DEMO_PROVIDER},broken")
    report = PL.preload_from_env(env)

    assert report.ran is True
    assert report.n_prepared == 1, report.as_dict()
    assert report.prepared[0]["key"] == key
    reasons = [row.get("reason", "") for row in report.skipped]
    assert any("test" in reason or "unusable" in reason for reason in reasons), report.as_dict()
    assert len(report.provider_material) == 1


def test_an_entry_whose_own_key_does_not_re_derive_is_not_prepared(tmp_path: Path, monkeypatch):
    _host, directory, env, key, _cold = prepare(tmp_path, monkeypatch)
    target = directory / f"profile_{key[:24]}.json"
    entry = json.loads(target.read_text(encoding="utf-8"))
    entry["cache_key"] = "f" * 64
    target.write_text(json.dumps(entry, indent=2, sort_keys=True), encoding="utf-8")

    report = PL.preload_from_env(env)
    assert report.n_prepared == 0
    rules = {row.get("rule") for row in report.skipped}
    assert "preload.self_key" in rules or "preload.filename" in rules, report.as_dict()


def test_a_prepared_profile_is_not_mutated_by_a_served_call(tmp_path: Path, monkeypatch):
    _host, directory, env, key, cold = prepare(tmp_path, monkeypatch)
    assert PL.preload_from_env(env).n_prepared == 1
    entry = PL.prepared_profiles().lookup(directory, key)
    assert entry is not None
    before = C.profile_content_hash(entry.profile)

    cache = C.ProfileCache(provider_id=F.DEMO_PROVIDER, directory=directory)
    cache.begin_call()
    loaded = cache.load(key, identity=cold.identity)
    assert loaded.profile is not None
    indexed = B.indexed_controls(loaded.profile)
    # the engine's own binding step: it must copy, never write into the served profile
    bound, _report = B.bind_profile(
        loaded.profile,
        indexed=indexed,
        names=[None] * len(indexed),
        idents=[None] * len(indexed),
    )
    assert C.profile_content_hash(loaded.profile) == before
    assert C.profile_content_hash(entry.profile) == before
    assert bound is not loaded.profile


def test_the_fast_path_refuses_when_the_hook_raises(tmp_path: Path):
    """A typed provider refusal on the fast path is surfaced, never swallowed."""
    host = MH.MockHostBridge()
    provider = BrokenMaterialProvider()
    with pytest.raises(IncompatibleSettings):
        resolve_instance_profile(host, provider, cache=make_cache(tmp_path / "cache", provider))
