"""P24 tests, workstream B: a **second** provider through the real cache/preload paths.

The uncertainty this file answers. P21-P23 built and demonstrated the capability
cache, the identity/source/revision invalidation, the startup preload and the warm
lookup - all of it against *one* provider. A working Vital run does not show that the
shared layer is provider-agnostic; it shows that it works for Vital. So this file runs
a deliberately *different* provider through the same production code and asks the same
questions:

1. does it build a schema-valid profile with its own identity, revision, sources and
   cache key (`test_the_second_provider_builds_a_valid_profile_...`);
2. does the key move with **every** determinant - revision, source digests, artifact
   digest, live fingerprint (`test_the_cache_key_moves_with_every_determinant_...`);
3. do two providers' entries coexist in one cache without shadowing or serving each
   other's profile (`test_the_two_providers_coexist_in_one_cache_...`);
4. does the **real** `create_server()` startup load a prepared profile of the second
   provider into memory, and does the next warm call serve it with zero builds and
   zero disk reads (`test_create_server_preloads_both_providers_...`);
5. do stale/incompatible entries of one provider leave the other's loaded
   (`test_a_stale_second_provider_entry_...`);
6. are *values* still read from the host live, and are writes still refused by the
   provider's own channel rules (`test_a_live_read_...`, `test_a_write_...`);
7. what happens when a plugin is identified but has no provider at all
   (`test_a_plugin_with_no_provider_configured_...`).

The provider itself lives in `second_provider.py` beside this file and is **not**
registered in the production provider map: the tests inject a factory into the
registry's private map at runtime, so this is a test-only provider by construction.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import mock_host as MH
import second_provider as SP

from reaper_reapy_mcp.plugins import api as A
from reaper_reapy_mcp.plugins import cache as C
from reaper_reapy_mcp.plugins import preload as PL
from reaper_reapy_mcp.plugins import profile as P
from reaper_reapy_mcp.plugins.providers import registry as provider_registry
from reaper_reapy_mcp.plugins.providers.vital import profile_build as PB
from reaper_reapy_mcp.plugins.providers.vital.provider import VitalProvider
from reaper_reapy_mcp.plugins.resolve import resolve_instance_profile

FIXTURES = Path(__file__).resolve().parent / "fixtures"
VITAL_REGISTRY = FIXTURES / "vital_registry"
NOT_THERE = FIXTURES / "no_calibration_configured.json"

VITAL_IDENTITY = {
    "fx_name": PB.VITAL_FX_NAME,
    "class_id": PB.VITAL_CLASS_ID,
    "module": PB.VITAL_MODULE,
    "param_count": 8,
}
SECOND_IDENTITY = {
    "fx_name": "VST3i: Second Test Synth",
    "class_id": "00112233445566778899AABBCCDDEEFF",
    "module": "second_test.vst3",
    "param_count": 8,
}
SECOND_HOST_NAMES = {2: "Gain", 4: "Shape"}
VITAL_HOST_NAMES = {
    0: "Beats Per Minute",
    1: "Chorus Filter Cutoff",
    2: "Chorus Delay 1",
    3: "Chorus Delay 2",
    4: "Chorus Mix",
    5: "Chorus Feedback",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def second_provider_registered(monkeypatch):
    """Register the test provider in the registry map *for this test only*.

    The production map is untouched: a fresh process does not know this id, which is
    the point - a test provider must not become a supported one by being referenced.
    """
    monkeypatch.setitem(provider_registry._FACTORIES, SP.PROVIDER_ID, SP.SecondProvider)
    monkeypatch.setattr(A, "_REGISTRY", None)
    yield


def second_slot(values: dict[str, float] | None = None) -> MH.MockFxSlot:
    return MH.MockFxSlot(
        name=SECOND_IDENTITY["fx_name"],
        module=SECOND_IDENTITY["module"],
        class_id=SECOND_IDENTITY["class_id"],
        params=dict(values or {str(2000 + index): 0.25 for index in range(8)}),
        param_names=dict(SECOND_HOST_NAMES),
    )


def vital_slot() -> MH.MockFxSlot:
    return MH.MockFxSlot(
        name=VITAL_IDENTITY["fx_name"],
        module=VITAL_IDENTITY["module"],
        class_id=VITAL_IDENTITY["class_id"],
        params={str(1000 + index): 0.25 for index in range(8)},
        param_names=dict(VITAL_HOST_NAMES),
    )


def both_in_one_track() -> MH.MockHostBridge:
    """One track, two plugins: what coexistence means for the live path."""
    return MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", [vital_slot(), second_slot()])])


def isolated(monkeypatch, cache_dir: Path, providers: str = "vital,second") -> None:
    monkeypatch.setenv("REAPER_MCP_PLUGIN_PROVIDERS", providers)
    monkeypatch.setenv("REAPER_MCP_VITAL_REGISTRY_DIR", str(VITAL_REGISTRY))
    monkeypatch.setenv("REAPER_MCP_VITAL_CALIBRATION", str(NOT_THERE))
    monkeypatch.setenv("REAPER_MCP_VITAL_ENUM_BOUNDARIES", str(NOT_THERE))
    monkeypatch.setenv("REAPER_MCP_PLUGIN_CACHE", str(cache_dir))


def second_provider(revision: str | None = None) -> SP.SecondProvider:
    return SP.SecondProvider(revision=revision)


def vital_provider() -> VitalProvider:
    return VitalProvider(registry=PB.load_registry(directory=str(VITAL_REGISTRY)))


def cold_build(cache_dir: Path, provider=None, host=None):
    """Build and save one entry through the real resolve path."""
    host = host if host is not None else MH.MockHostBridge(tracks=[MH.MockTrack(0, "T", [second_slot()])])
    provider = provider if provider is not None else second_provider()
    cache = C.ProfileCache(provider_id=str(provider.id), directory=cache_dir)
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
        counter = self

        def counting(self_path: Path, *args, **kwargs):
            try:
                if Path(self_path).parent == self.directory:
                    counter.count += 1
            except OSError:  # pragma: no cover - defensive only
                pass
            return original(self_path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", counting)


# ---------------------------------------------------------------------------
# 1. Identity, provenance, key determinants
# ---------------------------------------------------------------------------


def test_the_second_provider_builds_a_valid_profile_with_its_own_identity_and_provenance():
    provider = second_provider()
    build = C.build_capability_profile(provider, dict(SECOND_IDENTITY))
    profile = build.profile
    assert P.validate_profile(profile).ok, P.validate_profile(profile).codes()
    assert profile["provider"] == SP.PROVIDER_ID
    assert profile["identity"]["class_id"] == SECOND_IDENTITY["class_id"]
    assert profile["identity"]["compatible_versions"] == ["2.4.0"]
    assert profile["provenance"]["provider_revision"] == SP.IMPLEMENTATION_REVISION
    provenance = profile["provenance"]
    assert {row["file"] for row in provenance["sources"]} == set(SP.SOURCES)
    assert len(provenance["provider_artifact_digest"]) == 64
    assert len(provenance["source_digest"]) == 64
    # it is *not* the first provider's profile shape: no state channel, no routes
    assert profile["state"]["supported"] is False
    assert profile["modulation"] == {"supported": False}
    # and the identity the runtime compares against is the one it declares
    disagreements = C.identity_disagreements(profile, dict(SECOND_IDENTITY))
    assert disagreements == {}, disagreements


def test_the_cache_key_moves_with_every_determinant_of_the_second_provider():
    provider = second_provider()
    identity = dict(SECOND_IDENTITY)
    base = C.key_material_of(provider, identity).key_for(provider.id, identity)

    # 1. the provider's own revision
    other_revision = C.key_material_of(second_provider(revision="second-provider-r2"), identity)
    assert other_revision.key_for(provider.id, identity) != base, "a new revision must invalidate"

    # 2. the live fingerprint (a different plugin instance fingerprint is another key)
    other_fingerprint = C.key_material_of(provider, {**identity, "param_count": 9})
    assert other_fingerprint.key_for(provider.id, {**identity, "param_count": 9}) != base

    # 3. the provider id itself
    assert C.key_material_of(provider, identity).key_for("vital", identity) != base

    # 4. the *source* digests: rewriting one of the provider's two files moves the key
    import importlib

    notes_path = FIXTURES / "second_provider" / "second_notes.json"
    # bytes, not text: `read_text`/`write_text` translate line ends on Windows, so a
    # text round trip is only byte-preserving by accident of the file's current
    # convention - and this file is untracked, so a changed digest has no git recovery
    original_bytes = notes_path.read_bytes()
    before = C.key_material_of(provider, identity).source_digest
    try:
        notes_path.write_bytes(original_bytes.replace(b'"Soft"', b'"Soft '))
        assert notes_path.read_bytes() != original_bytes, "the rewrite must change the bytes"
        changed = C.key_material_of(provider, identity)
        assert changed.source_digest != before, (
            "rewriting a declared source file must move the source digest the key is"
            " built from (this assertion used to compare against a literal, which is"
            " satisfied by any digest at all)"
        )
        assert changed.key_for(provider.id, identity) != base
    finally:
        notes_path.write_bytes(original_bytes)
    assert notes_path.read_bytes() == original_bytes, "the fixture must be restored byte-exactly"
    importlib.reload(SP)
    assert C.key_material_of(second_provider(), identity).source_digest == C.key_material_of(
        provider, identity
    ).source_digest, "the restored file must reproduce the original digest"


def test_the_two_providers_coexist_in_one_cache_without_shadowing(tmp_path: Path, monkeypatch):
    cache_dir = tmp_path / "cache"
    isolated(monkeypatch, cache_dir)
    _host, second = cold_build(cache_dir)
    _host2, vital = cold_build(
        cache_dir,
        provider=vital_provider(),
        host=MH.MockHostBridge(tracks=[MH.MockTrack(0, "T", [vital_slot()])]),
    )
    assert second.cache.key != vital.cache.key
    files = sorted(path.name for path in cache_dir.glob("profile_*.json"))
    assert len(files) == 2, files

    # each key addresses its own provider's profile, and only its own
    second_cache = C.ProfileCache(provider_id=SP.PROVIDER_ID, directory=cache_dir)
    loaded_second = second_cache.load(second.cache.key)
    assert loaded_second.status == "hit"
    assert (loaded_second.profile or {})["provider"] == SP.PROVIDER_ID
    vital_cache = C.ProfileCache(provider_id="vital", directory=cache_dir)
    loaded_vital = vital_cache.load(vital.cache.key)
    assert (loaded_vital.profile or {})["provider"] == "vital"

    # ... and a provider addressing the *other* provider's key must not be served it
    # (found by this test: the first version returned the other profile as a hit)
    crossed = second_cache.load(vital.cache.key)
    assert crossed.profile is None and crossed.status == "refused", crossed.status
    assert any(refusal.rule == "provider_id" for refusal in second_cache.status.refusals)
    # the refusal must not destroy the entry it refused: it is valid for its owner
    assert second_cache.status.quarantined == []
    assert sorted(path.name for path in cache_dir.glob("profile_*.json")) == files
    assert vital_cache.load(vital.cache.key).status == "hit"

    # and a *save* of a foreign profile under this cache is refused too
    before = sorted(path.name for path in cache_dir.glob("profile_*.json"))
    foreign = C.ProfileCache(provider_id="vital", directory=cache_dir)
    status = foreign.save("0" * 64, second.profile)
    assert any(refusal.rule == "provider_id" for refusal in status.refusals), status.as_dict()
    assert sorted(path.name for path in cache_dir.glob("profile_*.json")) == before, (
        "a refused save must write nothing"
    )


# ---------------------------------------------------------------------------
# 2. Startup preload, warm reuse, costs
# ---------------------------------------------------------------------------


def test_create_server_preloads_both_providers_and_the_second_warms_without_a_build_or_a_read(
    tmp_path: Path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    isolated(monkeypatch, cache_dir)
    _host, second_cold = cold_build(cache_dir)
    _host2, vital_cold = cold_build(
        cache_dir,
        provider=vital_provider(),
        host=MH.MockHostBridge(tracks=[MH.MockTrack(0, "T", [vital_slot()])]),
    )

    PL.prepared_profiles().clear()
    started = time.perf_counter()
    server = start_server(monkeypatch)
    startup_seconds = time.perf_counter() - started
    assert server is not None
    report = PL.prepared_profiles().report
    assert report is not None and report.ran is True
    assert report.files_scanned == 2, report.as_dict()
    assert report.n_prepared == 2, report.as_dict()
    prepared_ids = sorted(row["key"] for row in report.prepared)
    assert prepared_ids == sorted([second_cold.cache.key, vital_cold.cache.key])

    counter = ReadCounter(monkeypatch, cache_dir)
    builds = {"n": 0}

    class Counting(SP.SecondProvider):
        def build_profile(self, identity):  # type: ignore[override]
            builds["n"] += 1
            return super().build_profile(identity)

    host = MH.MockHostBridge(tracks=[MH.MockTrack(0, "T", [second_slot()])])
    cache = C.ProfileCache(provider_id=SP.PROVIDER_ID, directory=cache_dir)
    warm_started = time.perf_counter()
    resolution = resolve_instance_profile(host, Counting(), cache=cache)
    warm_seconds = time.perf_counter() - warm_started

    assert resolution.cache.source == "cache"
    assert resolution.cache.key == second_cold.cache.key
    assert cache.status.prepared is True, "the startup index must answer the second provider too"
    assert cache.status.built is False
    assert cache.status.key_without_build is True
    assert builds["n"] == 0, "a prepared hit performs no build for any provider"
    assert counter.count == 0, "and reads no cache file again"
    assert C.profile_content_hash(resolution.profile) == C.profile_content_hash(
        second_cold.profile
    )
    assert startup_seconds < 5.0 and warm_seconds < 5.0, (startup_seconds, warm_seconds)

    # the costs, stated rather than implied: a warm hit is not a build
    assert warm_seconds < startup_seconds + 1.0


def test_a_stale_second_provider_entry_is_skipped_while_the_vital_one_stays_loaded(
    tmp_path: Path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    isolated(monkeypatch, cache_dir)
    _host, second_cold = cold_build(cache_dir)
    _host2, vital_cold = cold_build(
        cache_dir,
        provider=vital_provider(),
        host=MH.MockHostBridge(tracks=[MH.MockTrack(0, "T", [vital_slot()])]),
    )
    stale = json.loads(json.dumps(second_cold.profile))
    stale["provenance"]["provider_revision"] = "second-provider-r0"
    (cache_dir / "profile_stale0000000000000000.json").write_text(
        json.dumps(stale), encoding="utf-8"
    )
    schema_v1 = json.loads(json.dumps(second_cold.profile))
    schema_v1["profile_schema_version"] = 1
    (cache_dir / "profile_v1000000000000000000.json").write_text(
        json.dumps(schema_v1), encoding="utf-8"
    )

    PL.prepared_profiles().clear()
    start_server(monkeypatch)
    report = PL.prepared_profiles().report
    assert report.files_scanned == 4, report.as_dict()
    assert report.n_prepared == 2, report.as_dict()
    assert report.n_skipped == 2, report.as_dict()
    prepared = {row["key"] for row in report.prepared}
    assert prepared == {second_cold.cache.key, vital_cold.cache.key}
    # the two bad entries are still there: nothing deletes a user's cache at startup
    assert (cache_dir / "profile_stale0000000000000000.json").exists()
    assert (cache_dir / "profile_v1000000000000000000.json").exists()


# ---------------------------------------------------------------------------
# 3. Live values, and the write rules of a parameter-only provider
# ---------------------------------------------------------------------------


def test_a_live_read_for_the_second_provider_comes_from_the_host(tmp_path: Path, monkeypatch):
    cache_dir = tmp_path / "cache"
    isolated(monkeypatch, cache_dir)
    cold_build(cache_dir)
    PL.prepared_profiles().clear()
    start_server(monkeypatch)
    assert PL.prepared_profiles().report.n_prepared == 1

    host = MH.MockHostBridge(tracks=[MH.MockTrack(0, "T", [second_slot()])])
    monkeypatch.setattr(A, "_BRIDGE", host)
    config = A.PluginConfig(provider_ids=(SP.PROVIDER_ID,), cache_dir=str(cache_dir), env={})

    first = A.plugin_read(
        config=config, instance={"track_index": 0, "fx_index": 0}, controls=["second_gain"]
    )
    assert first["ok"] is True, first
    assert first["meta"]["cache"]["source"] == "cache"
    assert first["meta"]["cache"]["prepared"] is True
    assert first["meta"]["cache"]["built"] is False
    assert first["data"]["values"]["second_gain"]["normalized"] == pytest.approx(0.25)

    slot = host.tracks[0].slots[0]
    slot.params[slot.ident_by_index[2]] = 0.75
    second = A.plugin_read(
        config=config, instance={"track_index": 0, "fx_index": 0}, controls=["second_gain"]
    )
    assert second["data"]["values"]["second_gain"]["normalized"] == pytest.approx(0.75)
    assert host.count("read_params") >= 2, "every call reads the host, none caches a value"


def test_a_write_for_the_second_provider_is_refused_by_its_own_channel_rules(
    tmp_path: Path, monkeypatch
):
    """The write path's safety rules are provider-independent; the provider may refuse.

    Two layers can refuse here and both are correct: the shared write gate, when the
    live identity lacks a field it requires (measured on the mock host, which reports
    no version), and the provider's own request normalisation, which this
    parameter-only test provider implements as a typed refusal. What must never happen
    is a write reaching the host without a plan - that is what is asserted.
    """
    from reaper_reapy_mcp.plugins.errors import PluginError

    cache_dir = tmp_path / "cache"
    isolated(monkeypatch, cache_dir)
    cold_build(cache_dir)
    host = MH.MockHostBridge(tracks=[MH.MockTrack(0, "T", [second_slot()])])
    monkeypatch.setattr(A, "_BRIDGE", host)
    config = A.PluginConfig(provider_ids=(SP.PROVIDER_ID,), cache_dir=str(cache_dir), env={})

    with pytest.raises(PluginError) as refusal:
        A.plugin_write(
            config=config,
            instance={"track_index": 0, "fx_index": 0},
            request={"controls": {"second_gain": 3.0}},
            dry_run=False,
        )
    assert host.count("write_params") == 0, "a refused write must not reach the host"
    assert host.count("write_state_slice") == 0
    assert refusal.value.code, "the refusal must be typed"


def test_a_plugin_with_no_provider_configured_is_a_typed_refusal(tmp_path: Path, monkeypatch):
    """A plugin the configured providers do not own is refused, not guessed at.

    P28: the refusal is now the *generic* path's - the addressed slot is resolved
    (read-only identity) and, having no prepared discovery artifact, the call stops
    with `DiscoveryArtifactMissing` and the `plugin_prepare` remedy. What the test
    guards is unchanged and is what matters: nothing is invented for an unmeasured
    plugin - no write, no profile, no measurement.
    """
    from reaper_reapy_mcp.plugins.errors import DiscoveryArtifactMissing

    cache_dir = tmp_path / "cache"
    isolated(monkeypatch, cache_dir, providers="vital")
    monkeypatch.setenv("REAPER_MCP_PLUGIN_DISCOVERY", str(tmp_path / "discovery"))
    host = MH.MockHostBridge(tracks=[MH.MockTrack(0, "T", [second_slot()])])
    monkeypatch.setattr(A, "_BRIDGE", host)
    config = A.PluginConfig(provider_ids=("vital",), cache_dir=str(cache_dir), env={})
    with pytest.raises(DiscoveryArtifactMissing) as refusal:
        A.plugin_read(
            config=config, instance={"track_index": 0, "fx_index": 0}, controls=["second_gain"]
        )
    assert refusal.value.detail["remedy"].startswith("call plugin_prepare")
    assert host.count("write_params") == 0
    assert not list(cache_dir.glob("profile_*.json")), "no profile is invented for an unknown plugin"
    assert not list((tmp_path / "discovery").glob("discovery_*.json")), (
        "a read must never measure the plugin into an artifact"
    )


def test_two_instances_of_two_providers_in_one_track_do_not_collide(tmp_path: Path, monkeypatch):
    """The live path keeps the two apart: the right provider answers the right slot.

    With two providers configured, an instance must name the provider it belongs to -
    a typed refusal otherwise (measured while writing this test: ``several providers
    are configured and the instance names none``). That is the multi-provider protocol,
    and it is what stops one plugin being read through another provider's profile.
    """
    from reaper_reapy_mcp.plugins.errors import UnsupportedProvider

    cache_dir = tmp_path / "cache"
    isolated(monkeypatch, cache_dir)
    host = both_in_one_track()
    monkeypatch.setattr(A, "_BRIDGE", host)
    config = A.PluginConfig(provider_ids=("vital", SP.PROVIDER_ID), cache_dir=str(cache_dir), env={})

    vital_result = A.plugin_read(
        config=config,
        instance={"track_index": 0, "fx_index": 0, "provider": "vital"},
        controls=["beats_per_minute"],
    )
    assert vital_result["ok"] is True, vital_result
    assert vital_result["data"]["values"]["beats_per_minute"]["normalized"] == pytest.approx(0.25)

    second_result = A.plugin_read(
        config=config,
        instance={"track_index": 0, "fx_index": 1, "provider": SP.PROVIDER_ID},
        controls=["second_gain"],
    )
    assert second_result["ok"] is True, second_result
    assert second_result["data"]["values"]["second_gain"]["normalized"] == pytest.approx(0.25)

    # an instance that names no provider while several are configured is refused
    with pytest.raises(UnsupportedProvider):
        A.plugin_read(
            config=config, instance={"track_index": 0, "fx_index": 0}, controls=["second_gain"]
        )
    assert host.count("write_params") == 0
