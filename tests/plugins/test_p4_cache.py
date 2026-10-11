"""P4 tests: capability-profile building and the profile cache (07, 09).

Covers the [09 §9] matrix: key stability and the **B-47 change matrix**, the
five refusals with the **B-55** byte-compare quarantine proof, the **B-56**
interrupted write, truncation failing two independent checks, the fail-closed
identity rule (AUD-008) and the hot-path assertion (a cache hit performs zero
host parameter reads).

Everything here runs offline against the mock provider and the mock host; no
test touches the real cache directory or any host.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

import fixtures as F
import mock_host as MH
from mock_provider import MockProvider

from reaper_reapy_mcp.plugins import cache as C
from reaper_reapy_mcp.plugins.errors import IncompatibleSettings, SnapshotUnavailable, VersionMismatch
from reaper_reapy_mcp.plugins.model import canonical_json
from reaper_reapy_mcp.plugins.resolve import identity_missing_fields, resolve_instance_profile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class VariantProvider(MockProvider):
    """A mock provider whose cache determinants can be moved one at a time."""

    def __init__(
        self,
        *,
        data_file: Path | None = None,
        artifact_files: list[Path] | None = None,
        revision: str | None = None,
        profile_mutator=None,
    ) -> None:
        super().__init__()
        self._data_file = data_file
        self._artifact_files = artifact_files
        self._profile_mutator = profile_mutator
        if revision is not None:
            self.implementation_revision = revision

    def artifact_paths(self) -> list[str]:
        if self._artifact_files is not None:
            return [str(path) for path in self._artifact_files]
        return super().artifact_paths()

    def build_profile(self, identity):
        profile = super().build_profile(identity)
        if self._data_file is not None:
            profile["provenance"]["sources"] = [C.source_row(self._data_file)]
        if self._profile_mutator is not None:
            self._profile_mutator(profile)
        return profile


def make_cache(tmp_path: Path, provider: MockProvider, *, now=None) -> C.ProfileCache:
    return C.ProfileCache(
        provider_id=str(provider.id), directory=tmp_path / "cache", now=now
    )


def resolve(bridge, provider, cache, **kwargs):
    return resolve_instance_profile(bridge, provider, cache=cache, **kwargs)


def key_for(provider, cache_dir_identity, **overrides):
    build = C.build_capability_profile(provider, cache_dir_identity)
    material = {
        "provider_id": str(provider.id),
        "identity": cache_dir_identity,
        "provider_revision": build.provider_revision,
        "provider_artifact_digest": build.provider_artifact_digest,
        "source_digest": build.source_digest,
    }
    material.update(overrides)
    return C.compute_cache_key(**material), build


def write_entry(cache: C.ProfileCache, key: str, profile: dict) -> Path:
    cache.save(key, profile)
    target = cache.path_for(key)
    assert target.exists()
    return target


# ---------------------------------------------------------------------------
# Directory policy (09 §4, AUD-014 Q-07)
# ---------------------------------------------------------------------------


def test_default_cache_directory_is_user_local_and_outside_a_source_tree():
    default = C.cache_directory({})
    assert "reaper_reapy_mcp" in str(default).lower()
    assert C.source_tree_marker(default) is None, (
        "the default cache directory must not be inside a source-control tree (09 §4)"
    )
    repo = Path(__file__).resolve().parents[2]
    assert C.source_tree_marker(repo) is not None, (
        "non-vacuity: the repository root itself must be detected as a source tree"
    )
    assert repo not in default.parents


def test_cache_directory_env_override_is_absolute_and_empty_value_is_refused(tmp_path):
    override = tmp_path / "explicit" / ".." / "explicit2"
    resolved = C.cache_directory({C.CACHE_DIR_ENV: str(override)})
    assert resolved == Path(os.path.normpath(os.path.abspath(str(override))))
    assert resolved.is_absolute()
    with pytest.raises(IncompatibleSettings) as excinfo:
        C.cache_directory({C.CACHE_DIR_ENV: "   "})
    assert excinfo.value.code == "IncompatibleSettings"
    assert excinfo.value.detail["variable"] == C.CACHE_DIR_ENV


def test_configured_directory_inside_a_source_tree_is_warned_about(tmp_path):
    inside = tmp_path / "repo" / "sub"
    inside.mkdir(parents=True)
    (tmp_path / "repo" / ".git").mkdir()
    cache = C.ProfileCache(provider_id="demo", directory=inside)
    assert any("source-control tree" in row["reason"] for row in cache.status.warnings), (
        "a configured cache inside a git tree must warn (09 §4)"
    )
    outside = tmp_path / "plain"
    outside.mkdir()
    quiet = C.ProfileCache(provider_id="demo", directory=outside)
    assert quiet.status.warnings == []


# ---------------------------------------------------------------------------
# Key stability and the B-47 change matrix (AUD-008)
# ---------------------------------------------------------------------------


def test_key_is_stable_for_identical_determinants():
    provider = MockProvider()
    identity = F.demo_facts()
    first, _ = key_for(provider, identity)
    second, _ = key_for(provider, identity)
    assert first == second
    assert len(first) == 64
    # a fresh build of the same provider differs only in the volatile stamp and
    # therefore keeps both the key and the content hash
    build_a = C.build_capability_profile(provider, identity, now="2026-01-01T00:00:00Z")
    build_b = C.build_capability_profile(provider, identity, now="2027-02-02T02:02:02Z")
    assert build_a.key_for(provider.id, identity) == build_b.key_for(provider.id, identity)
    assert C.profile_content_hash(build_a.profile) == C.profile_content_hash(build_b.profile)
    assert canonical_json(build_a.profile) != canonical_json(build_b.profile)


def test_b47_every_determinant_changes_the_key(tmp_path):
    data_file = tmp_path / "registry.json"
    data_file.write_text('{"controls": 1}', encoding="utf-8")
    artifact = tmp_path / "provider_logic.py"
    artifact.write_text("RULE_CONTENT = 1\n", encoding="utf-8")
    identity = F.demo_facts()

    baseline, _ = key_for(
        VariantProvider(data_file=data_file, artifact_files=[artifact], revision="r1"), identity
    )

    # 1. fingerprint (identity) change
    other_identity = dict(identity)
    other_identity["param_count"] = identity["param_count"] + 1
    fingerprint_key, _ = key_for(
        VariantProvider(data_file=data_file, artifact_files=[artifact], revision="r1"),
        other_identity,
    )
    # 2. registry data change (source digest)
    data_file.write_text('{"controls": 2}', encoding="utf-8")
    data_key, _ = key_for(
        VariantProvider(data_file=data_file, artifact_files=[artifact], revision="r1"), identity
    )
    # 3. provider revision change (provider version / logic release)
    revision_key, _ = key_for(
        VariantProvider(data_file=data_file, artifact_files=[artifact], revision="r2"), identity
    )
    # 4. artifact digest change (dependency rules / codec / verification policy)
    artifact.write_text("RULE_CONTENT = 2\n", encoding="utf-8")
    artifact_key, _ = key_for(
        VariantProvider(data_file=data_file, artifact_files=[artifact], revision="r2"), identity
    )
    # 5. schema / format key change
    schema_key, build = key_for(
        VariantProvider(data_file=data_file, artifact_files=[artifact], revision="r2"), identity
    )
    schema_key = C.compute_cache_key(
        provider_id="demo",
        identity=identity,
        provider_revision=build.provider_revision,
        provider_artifact_digest=build.provider_artifact_digest,
        source_digest=build.source_digest,
        schema_version=C.PROFILE_SCHEMA_VERSION + 1,
    )
    format_key = C.compute_cache_key(
        provider_id="demo",
        identity=identity,
        provider_revision=build.provider_revision,
        provider_artifact_digest=build.provider_artifact_digest,
        source_digest=build.source_digest,
        cache_format_key="capability-map-v99",
    )

    keys = {
        "baseline": baseline,
        "fingerprint": fingerprint_key,
        "registry_data": data_key,
        "provider_revision": revision_key,
        "artifact_digest": artifact_key,
        "schema_version": schema_key,
        "format_key": format_key,
    }
    assert len(set(keys.values())) == len(keys), f"every determinant must be load-bearing: {keys}"


def test_b47_old_profile_is_never_served_after_a_determinant_changes(tmp_path):
    data_file = tmp_path / "registry.json"
    data_file.write_text('{"controls": 1}', encoding="utf-8")
    provider_a = VariantProvider(data_file=data_file, revision="r1")
    cache = make_cache(tmp_path, provider_a)
    bridge = MH.MockHostBridge()

    first = resolve(bridge, provider_a, cache)
    assert first.cache.source == "built"
    first_file = cache.path_for(first.cache.key)
    first_bytes = first_file.read_bytes()

    # the registry data is regenerated: a new source digest, a new key
    data_file.write_text('{"controls": 2}', encoding="utf-8")
    provider_b = VariantProvider(data_file=data_file, revision="r1")
    second = resolve(bridge, provider_b, cache)

    assert second.cache.key != first.cache.key, "a regenerated source must produce a new key"
    assert second.cache.source == "built"
    assert second.profile["provenance"]["source_digest"] != first.profile["provenance"]["source_digest"]
    # the old entry is untouched and no longer reachable by the new key
    assert first_file.read_bytes() == first_bytes
    assert len(C.entry_files_in(cache.directory)) == 2
    third = resolve(bridge, provider_b, cache)
    assert third.cache.source == "cache"
    assert third.cache.key == second.cache.key


def test_provider_without_revision_or_readable_sources_is_not_cacheable(tmp_path):
    cache = make_cache(tmp_path, MockProvider())
    provider = VariantProvider(revision="")
    build = C.build_capability_profile(provider, F.demo_facts())
    assert build.validation.rebuild_required is False
    assert build.cacheable is False
    key, _ = key_for(provider, F.demo_facts())
    cache.save(key, build.profile)
    assert cache.status.refusals and cache.status.refusals[0].rule == "not_cacheable"
    assert C.entry_files_in(cache.directory) == []

    missing_source = tmp_path / "gone.json"
    unreadable = VariantProvider(data_file=missing_source, revision="r1")
    build2 = C.build_capability_profile(unreadable, F.demo_facts())
    assert build2.cacheable is False
    assert any(
        row["sha256"] is None for row in build2.profile["provenance"]["sources"]
    ), "an unreadable source must be recorded as unreadable, not hashed as empty"
    assert C.entry_files_in(cache.directory) == []


# ---------------------------------------------------------------------------
# The five refusals (09 §5) and B-55 byte-compare quarantine (AUD-012)
# ---------------------------------------------------------------------------


def test_no_file_is_built_and_written_atomically(tmp_path):
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    resolution = resolve(MH.MockHostBridge(), provider, cache)
    assert resolution.cache.source == "built"
    assert resolution.cache.refusals == []
    entry = C.read_entry_file(resolution.cache.path)
    assert entry is not None
    assert entry["cache_key"] == resolution.cache.key
    assert entry["cache_format_key"] == C.CACHE_FORMAT_KEY
    assert entry["counts"] == C.profile_counts(entry["profile"])
    assert entry["content_sha256"] == C.profile_content_hash(entry["profile"])
    assert C.temp_files_in(cache.directory) == [], "a completed write leaves no temp file"


def test_unparsable_file_is_quarantined_and_rebuilt(tmp_path):
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    build = C.build_capability_profile(provider, F.demo_facts())
    key = build.key_for(provider.id, F.demo_facts())
    target = cache.path_for(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    original = b"{ this is not json"
    target.write_bytes(original)

    loaded = cache.load(key, identity=F.demo_facts())
    assert loaded.status == "refused"
    assert loaded.refusal is not None and loaded.refusal.rule == "unparsable"
    quarantine = Path(loaded.refusal.quarantine_path)
    assert quarantine.read_bytes() == original, "quarantined bytes must be byte-identical"
    assert not target.exists()

    cache.save(key, build.profile)
    assert target.exists()
    entry = C.read_entry_file(target)
    assert entry is not None and entry["cache_key"] == key
    assert quarantine.read_bytes() == original
    assert any("does not parse" in refusal.reason for refusal in cache.status.refusals)


def test_b55_foreign_file_at_expected_path_is_never_replaced_in_place(tmp_path):
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    build = C.build_capability_profile(provider, F.demo_facts())
    key = build.key_for(provider.id, F.demo_facts())
    target = cache.path_for(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    foreign = json.dumps(
        {"cache_format_key": C.CACHE_FORMAT_KEY, "cache_key": "not-our-key", "profile": {}}
    ).encode("utf-8")
    target.write_bytes(foreign)

    loaded = cache.load(key, identity=F.demo_facts())
    assert loaded.status == "refused"
    assert loaded.refusal is not None and loaded.refusal.rule == "foreign_key"
    quarantine = Path(loaded.refusal.quarantine_path)
    assert quarantine.read_bytes() == foreign  # bytes preserved, not copied-and-deleted
    assert quarantine.name.endswith(".json" + ".invalid-" + cache._stamp())

    cache.save(key, build.profile)
    entry = C.read_entry_file(target)
    assert entry is not None and entry["cache_key"] == key
    assert target.read_bytes() != foreign
    assert quarantine.read_bytes() == foreign
    assert str(quarantine) in cache.status.quarantined


def test_content_hash_and_counts_fail_as_two_independent_checks(tmp_path):
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    build = C.build_capability_profile(provider, F.demo_facts())
    key = build.key_for(provider.id, F.demo_facts())
    write_entry(cache, key, build.profile)
    target = cache.path_for(key)
    good = json.loads(target.read_text(encoding="utf-8"))
    good_text = target.read_text(encoding="utf-8")

    # (a) a lost tail without re-hashing: the content hash catches it
    truncated = copy.deepcopy(good)
    truncated["profile"]["controls"] = truncated["profile"]["controls"][:-1]
    a_bytes = json.dumps(truncated).encode("utf-8")
    target.write_bytes(a_bytes)
    first = cache.load(key, identity=F.demo_facts())
    assert first.status == "refused" and first.refusal.rule == "content_hash"

    # (b) a "reserialized" file whose content hash is recomputed but whose
    # counts are stale: only the count cross-check can catch it
    write_entry(cache, key, build.profile)
    stale = copy.deepcopy(good)
    stale["profile"]["controls"] = stale["profile"]["controls"][:-1]
    stale["content_sha256"] = C.profile_content_hash(stale["profile"])
    b_bytes = json.dumps(stale).encode("utf-8")
    target.write_bytes(b_bytes)
    second = cache.load(key, identity=F.demo_facts())
    assert second.status == "refused" and second.refusal.rule == "counts"

    # (c) a byte-level truncation is caught as unparsable
    write_entry(cache, key, build.profile)
    c_bytes = good_text[: len(good_text) // 2].encode("utf-8")
    target.write_bytes(c_bytes)
    third = cache.load(key, identity=F.demo_facts())
    assert third.status == "refused" and third.refusal.rule == "unparsable"

    # every refusal quarantined its own bytes: three files, three byte-exact
    # originals - a later quarantine must never overwrite an earlier one
    quarantines = C.quarantined_files_in(cache.directory)
    assert len(quarantines) == 3
    assert C.entry_files_in(cache.directory) == []
    assert sorted(path.read_bytes() for path in quarantines) == sorted([a_bytes, b_bytes, c_bytes])
    assert [
        first.refusal.quarantine_path,
        second.refusal.quarantine_path,
        third.refusal.quarantine_path,
    ] == [str(path) for path in quarantines]


def test_content_hash_mismatch_with_stale_hash_is_reported(tmp_path):
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    build = C.build_capability_profile(provider, F.demo_facts())
    key = build.key_for(provider.id, F.demo_facts())
    write_entry(cache, key, build.profile)
    target = cache.path_for(key)
    entry = json.loads(target.read_text(encoding="utf-8"))
    entry["content_sha256"] = "0" * 64
    target.write_text(json.dumps(entry), encoding="utf-8")
    loaded = cache.load(key, identity=F.demo_facts())
    assert loaded.status == "refused"
    assert loaded.refusal.rule == "content_hash"


def test_schema_format_and_identity_mismatches_quarantine(tmp_path):
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    build = C.build_capability_profile(provider, F.demo_facts())
    key = build.key_for(provider.id, F.demo_facts())
    target = write_entry(cache, key, build.profile)

    # format key mismatch
    entry = json.loads(target.read_text(encoding="utf-8"))
    entry["cache_format_key"] = "capability-map-v0"
    target.write_text(json.dumps(entry), encoding="utf-8")
    assert cache.load(key, identity=F.demo_facts()).refusal.rule == "format_key"

    # schema version mismatch
    target = write_entry(cache, key, build.profile)
    entry = json.loads(target.read_text(encoding="utf-8"))
    entry["profile_schema_version"] = 99
    target.write_text(json.dumps(entry), encoding="utf-8")
    assert cache.load(key, identity=F.demo_facts()).refusal.rule == "schema_version"

    # identity disagreement with the live instance
    target = write_entry(cache, key, build.profile)
    other = dict(F.demo_facts())
    other["class_id"] = F.FOREIGN_CLASS
    loaded = cache.load(key, identity=other)
    assert loaded.status == "refused"
    assert loaded.refusal.rule == "identity"
    assert "class_id" in loaded.refusal.reason
    assert not target.exists()


# ---------------------------------------------------------------------------
# B-56 interrupted write / crash safety
# ---------------------------------------------------------------------------


def test_b56_stray_temp_file_is_never_read_and_never_blocks(tmp_path):
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    build = C.build_capability_profile(provider, F.demo_facts())
    key = build.key_for(provider.id, F.demo_facts())
    cache.directory.mkdir(parents=True, exist_ok=True)
    stray = cache.directory / f"profile_{key[:12]}.json{C._TEMP_INFIX}999-deadbeef"
    stray.write_text("partial garbage", encoding="utf-8")

    loaded = cache.load(key, identity=F.demo_facts())
    assert loaded.status == "miss", "a temp file is not an entry"
    cache.save(key, build.profile)
    assert cache.path_for(key).exists()
    assert C.read_entry_file(cache.path_for(key)) is not None
    assert stray.read_text(encoding="utf-8") == "partial garbage"


def test_b56_failure_between_temp_write_and_replace_leaves_no_partial_target(tmp_path, monkeypatch):
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    build = C.build_capability_profile(provider, F.demo_facts())
    key = build.key_for(provider.id, F.demo_facts())
    target = cache.path_for(key)
    real_replace = os.replace

    def failing_replace(src, dst):
        if Path(dst) == target:
            raise OSError("simulated kill between temp write and replace")
        return real_replace(src, dst)

    monkeypatch.setattr(C.os, "replace", failing_replace)
    cache.save(key, build.profile)

    assert not target.exists(), "a partial target must never appear"
    assert any(refusal.rule == "write" for refusal in cache.status.refusals)
    for leftover in C.temp_files_in(cache.directory):
        assert not C.read_entry_file(leftover), "a temp file is never readable as an entry"
    # and the next (successful) save still works
    monkeypatch.setattr(C.os, "replace", real_replace)
    cache.save(key, build.profile)
    assert C.read_entry_file(target) is not None


def test_saving_the_same_profile_twice_is_idempotent(tmp_path):
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    build = C.build_capability_profile(provider, F.demo_facts())
    key = build.key_for(provider.id, F.demo_facts())
    write_entry(cache, key, build.profile)
    target = cache.path_for(key)
    before = target.read_bytes()
    cache.save(key, build.profile)
    assert target.read_bytes() == before
    assert len(C.entry_files_in(cache.directory)) == 1


def test_cache_problems_never_fail_a_call(tmp_path):
    blocker = tmp_path / "cache-is-a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    provider = MockProvider()
    cache = C.ProfileCache(provider_id=provider.id, directory=blocker)
    build = C.build_capability_profile(provider, F.demo_facts())
    key = build.key_for(provider.id, F.demo_facts())
    cache.save(key, build.profile)  # must not raise (09 §6)
    assert cache.status.refusals, "an unusable directory must be reported, not raised"
    assert cache.status.refusals[0].rule in ("directory", "write")


# ---------------------------------------------------------------------------
# Identity fail-closed (AUD-008) and the version gate (07 §3.2)
# ---------------------------------------------------------------------------


class NullParamCountBridge(MH.MockHostBridge):
    """A bridge whose param count is unreadable for the fixture slot."""

    def fx_facts(self, track_index: int, fx_index: int):
        facts = super().fx_facts(track_index, fx_index)
        facts.param_count = None
        return facts


def test_identity_missing_fields_detection():
    assert identity_missing_fields(F.demo_facts()) == []
    broken = dict(F.demo_facts())
    broken["param_count"] = None
    assert identity_missing_fields(broken) == ["param_count"]
    broken = dict(F.demo_facts())
    broken["class_id"] = "  "
    assert identity_missing_fields(broken) == ["class_id"]


def test_p4_5_no_identity_means_no_cache_file_and_writes_refused(tmp_path):
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    resolution = resolve(NullParamCountBridge(), provider, cache)

    assert resolution.missing_identity == ["param_count"]
    assert resolution.provisional is True
    assert resolution.usable_for_writes is False
    assert resolution.cache.source == "none"
    assert C.entry_files_in(cache.directory) == [], "no cache file may be written without identity"
    with pytest.raises(SnapshotUnavailable) as excinfo:
        resolution.require_writable()
    assert excinfo.value.detail["missing_fields"] == ["param_count"]
    assert excinfo.value.detail["provisional"] is True
    meta = resolution.meta()
    assert meta["provisional"] is True
    assert meta["cache"]["refusals"][0]["rule"] == "cache.identity"


def test_version_outside_the_measured_set_refuses_writes_without_override(tmp_path):
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    bridge = MH.MockHostBridge([MH.MockTrack(0, "Track 1", [MH.MockFxSlot(version="9.9.9")])])
    resolution = resolve(bridge, provider, cache)
    assert resolution.usable_for_writes is False
    assert resolution.provisional is False, "an unmeasured version is not a provisional read"
    with pytest.raises(VersionMismatch) as excinfo:
        resolution.require_writable()
    assert excinfo.value.detail["live_version"] == "9.9.9"
    assert excinfo.value.detail["override"] is None
    assert "9.9.9" in excinfo.value.message


def test_absent_version_is_tolerated_for_writes(tmp_path):
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    bridge = MH.MockHostBridge([MH.MockTrack(0, "Track 1", [MH.MockFxSlot(version=None)])])
    resolution = resolve(bridge, provider, cache)
    assert resolution.identity.get("version") is None
    assert resolution.usable_for_writes is True
    resolution.require_writable()


# ---------------------------------------------------------------------------
# Hot path (09 §9) and meta reporting
# ---------------------------------------------------------------------------


def test_hot_path_cache_hit_performs_zero_host_parameter_reads(tmp_path):
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    bridge = MH.MockHostBridge()

    first = resolve(bridge, provider, cache)
    assert first.cache.source == "built"
    bridge.reset_log()

    second = resolve(bridge, provider, cache)
    assert second.cache.source == "cache"
    assert second.cache.key == first.cache.key
    assert bridge.count("read_params") == 0, "a cache hit must not read parameters"
    assert bridge.count("param_idents") == 0
    assert bridge.count("fx_facts") >= 1, (
        "non-vacuity: identity still comes from fresh host facts, only the tax is gone"
    )
    meta = second.meta()
    assert meta["cache"]["source"] == "cache"
    assert meta["cache"]["key"] == first.cache.key
    assert meta["cache"]["path"]


def test_cache_status_reports_refusals_with_reasons_and_paths(tmp_path):
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    build = C.build_capability_profile(provider, F.demo_facts())
    key = build.key_for(provider.id, F.demo_facts())
    target = cache.path_for(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    foreign = {
        "cache_format_key": C.CACHE_FORMAT_KEY,
        "cache_key": "someone-elses-key",
        "profile_schema_version": C.PROFILE_SCHEMA_VERSION,
        "profile": {},
    }
    target.write_text(json.dumps(foreign), encoding="utf-8")
    cache.load(key, identity=F.demo_facts())
    row = cache.status.as_dict()
    assert row["refusals"][0]["rule"] == "cache.foreign_key"
    assert row["refusals"][0]["quarantine"]
    assert row["quarantined"] == [row["refusals"][0]["quarantine"]]
    assert ":" not in Path(row["quarantined"][0]).name, (
        "the quarantine stamp must be a legal file name on every supported OS"
    )


# ---------------------------------------------------------------------------
# Regression tests for the independent audit findings (2026-10-10)
# ---------------------------------------------------------------------------


def test_quarantine_uses_an_atomic_rename_and_never_a_copy(tmp_path, monkeypatch):
    """09 §5 write mechanics: quarantine is a rename, so the bytes cannot be lost
    mid-operation. A copy-and-delete implementation would pass a byte comparison
    and must fail here."""
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    build = C.build_capability_profile(provider, F.demo_facts())
    key = build.key_for(provider.id, F.demo_facts())
    target = cache.path_for(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"foreign bytes")

    renames: list[tuple[str, str]] = []
    real_rename = os.rename

    def spy(src, dst):
        renames.append((str(src), str(dst)))
        return real_rename(src, dst)

    monkeypatch.setattr(C.os, "rename", spy)
    loaded = cache.load(key, identity=F.demo_facts())
    assert loaded.status == "refused"
    assert renames == [(str(target), str(loaded.refusal.quarantine_path))], renames
    assert Path(loaded.refusal.quarantine_path).read_bytes() == b"foreign bytes"
    assert not target.exists(), "a rename leaves no copy behind"


def test_a_failed_quarantine_rename_never_leads_to_publishing_over_the_bytes(tmp_path, monkeypatch):
    """AUD-012: if the occupied bytes cannot be moved aside, nothing may be
    published over them - earlier behaviour destroyed the foreign file."""
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    build = C.build_capability_profile(provider, F.demo_facts())
    key = build.key_for(provider.id, F.demo_facts())
    target = cache.path_for(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"foreign bytes that must survive")

    def denied(src, dst):
        raise PermissionError("simulated share violation")

    monkeypatch.setattr(C.os, "rename", denied)
    loaded = cache.load(key, identity=F.demo_facts())
    assert loaded.status == "refused" and loaded.refusal.quarantine_path is None
    cache.save(key, build.profile)
    assert target.read_bytes() == b"foreign bytes that must survive", (
        "a failed quarantine must suppress the publish, not be followed by it"
    )
    assert any(refusal.rule == "occupied" for refusal in cache.status.refusals)
    assert C.quarantined_files_in(cache.directory) == [], "nothing was moved aside"
    assert len(C.entry_files_in(cache.directory)) == 1, (
        "the foreign file is still the only thing at the entry path"
    )


def test_an_entry_whose_provenance_does_not_derive_its_key_is_not_served(tmp_path):
    """AUD-008 at load time: the file must re-derive the key it is filed under."""
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    build = C.build_capability_profile(provider, F.demo_facts())
    key = build.key_for(provider.id, F.demo_facts())
    write_entry(cache, key, build.profile)
    target = cache.path_for(key)
    entry = json.loads(target.read_text(encoding="utf-8"))
    # re-seal the file so every self-consistency check passes, but with provenance
    # that no longer hashes to this key
    entry["profile"]["provenance"]["provider_revision"] = "a-different-revision"
    entry["content_sha256"] = C.profile_content_hash(entry["profile"])
    entry["counts"] = C.profile_counts(entry["profile"])
    target.write_text(json.dumps(entry), encoding="utf-8")

    loaded = cache.load(key, identity=F.demo_facts())
    assert loaded.status == "refused"
    assert loaded.refusal.rule == "key_material"


def test_an_entry_without_a_provider_id_is_not_served(tmp_path):
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    build = C.build_capability_profile(provider, F.demo_facts())
    key = build.key_for(provider.id, F.demo_facts())
    write_entry(cache, key, build.profile)
    target = cache.path_for(key)
    entry = json.loads(target.read_text(encoding="utf-8"))
    entry.pop("provider_id")
    target.write_text(json.dumps(entry), encoding="utf-8")
    loaded = cache.load(key, identity=F.demo_facts())
    assert loaded.status == "refused" and loaded.refusal.rule == "key_material"


def test_a_non_cacheable_entry_is_neither_written_nor_served(tmp_path):
    """09 §6: with the provider revision undetermined, no validated-profile cache
    entry is written **or served**."""
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    build = C.build_capability_profile(provider, F.demo_facts())
    key = build.key_for(provider.id, F.demo_facts())
    profile = copy.deepcopy(build.profile)
    profile["provenance"]["provider_revision"] = ""
    entry = {
        "cache_format_key": C.CACHE_FORMAT_KEY,
        "provider_id": provider.id,
        "cache_key": key,
        "profile_schema_version": C.PROFILE_SCHEMA_VERSION,
        "written_utc": "2026-01-01T00:00:00Z",
        "counts": C.profile_counts(profile),
        "content_sha256": C.profile_content_hash(profile),
        "profile": profile,
    }
    target = cache.path_for(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(entry), encoding="utf-8")
    loaded = cache.load(key, identity=F.demo_facts())
    assert loaded.status == "refused", "a non-cacheable profile must not be served"
    assert loaded.refusal.rule in ("not_cacheable", "key_material")
    assert not target.exists(), "it is quarantined, never reused"


def test_an_unserializable_profile_cannot_raise_out_of_save(tmp_path):
    provider = MockProvider()
    cache = make_cache(tmp_path, provider)
    build = C.build_capability_profile(provider, F.demo_facts())
    key = build.key_for(provider.id, F.demo_facts())
    profile = copy.deepcopy(build.profile)
    profile["x_unsafe"] = Path("not-serializable")
    cache.save(key, profile)  # must not raise (09 §6)
    assert any(refusal.rule == "not_serializable" for refusal in cache.status.refusals)
    assert C.entry_files_in(cache.directory) == []
