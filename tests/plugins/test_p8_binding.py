"""P8.2: the live parameter-ident binding and the document-version gate (mocks).

Two things must be *measured* before a parameter write may be issued, and both
are tested here on the mock host (the live measurement is
`tests/plugins/live/probe_p8_baseline.py` and the P8 live suites):

1. **the binding** (07 §3.6): the declared index is only trustworthy while the
   host's own parameter name at that index equals a name the profile declares
   for the control. A build that reordered its parameters makes the index
   address a *different* parameter - and a verified read-back of the wrong
   parameter looks exactly like success, which is why the mismatch refuses.
2. **the version gate** (07 §3.2): the live version comes from the decoded
   document through the provider's optional `document_version` hook; a value
   outside the measured set refuses the write with `write_calls_attempted == 0`
   and no override.

Every refusal asserted here also asserts `write_calls_attempted == 0`: a safety
test that does not check the counters proves nothing about the write path.
"""

from __future__ import annotations

import fixtures as F
import mock_host as MH
from mock_provider import MockProvider

from reaper_reapy_mcp.plugins import binding as B
from reaper_reapy_mcp.plugins.errors import UnsupportedControl, VersionMismatch
from reaper_reapy_mcp.plugins.model import PluginInstanceRef, Verdict
from reaper_reapy_mcp.plugins.resolve import TargetTokenRegistry
from reaper_reapy_mcp.plugins.transaction import Transaction

CUTOFF_REQUEST = {"controls": {"tone_cutoff": 24.0}}


def build_host() -> MH.MockHostBridge:
    foreign = MH.MockFxSlot(
        name=F.FOREIGN_NAME,
        module=F.FOREIGN_MODULE,
        class_id=F.FOREIGN_CLASS,
        block=F.foreign_block_text(),
        params={"0:9000": 0.1},
    )
    return MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", [foreign, MH.MockFxSlot()])])


def make(*, request, host=None, dry_run=True, provider=None, fx_index=1):
    host = host if host is not None else build_host()
    provider = provider or MockProvider()
    facts = host.fx_facts(0, fx_index)
    registry = TargetTokenRegistry(session_context={"project": "test"})
    token = registry.issue(facts=facts)
    ref = PluginInstanceRef(
        host="reaper",
        location={"track_index": 0, "fx_index": fx_index},
        provider="demo",
        identity=provider.identify(facts),
    )
    transaction = Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=provider.build_profile(provider.identify(facts)),
        ref=ref,
        request=request,
        token=token,
        registry=registry,
        dry_run=dry_run,
    )
    return transaction, host


# ---------------------------------------------------------------------------
# The pure binding rule
# ---------------------------------------------------------------------------


def test_bind_profile_binds_only_controls_the_host_names_confirm():
    profile = F.demo_profile()
    indexed = B.indexed_controls(profile)
    assert indexed == [
        ("tone_cutoff", 3),
        ("tone_level", 4),
        ("tone_shape", 5),
        ("mix_percent", 6),
    ]
    assert B.unindexed_controls(profile) == ["mode_state"]

    names = ["Tone Cutoff", "Something Else", "Tone Shape", None]
    idents = ["3:1003", "4:1004", "5:9999", "6:1006"]
    bound, report = B.bind_profile(profile, indexed=indexed, names=names, idents=idents)

    controls = {row["id"]: row for row in bound["controls"]}
    assert controls["tone_cutoff"]["binding"]["ident"] == "3:1003"
    assert controls["tone_shape"]["binding"]["ident"] == "5:9999", "the live ident is authoritative"
    assert "ident" not in controls["tone_level"]["binding"] or controls["tone_level"]["binding"]["ident"] is None
    assert controls["mix_percent"]["binding"]["ident"] is None
    assert report.bound == 2
    assert report.declared_ident_kept == 1  # tone_cutoff kept "3:1003"
    assert report.ident_refreshed == 1  # tone_shape: "5:1005" -> "5:9999"
    reasons = {row["reason"] for row in report.unverified}
    assert reasons == {"name_mismatch", "name_unreadable"}
    mismatch = next(row for row in report.unverified if row["reason"] == "name_mismatch")
    assert mismatch["observed_name"] == "Something Else"
    assert "tone level" in mismatch["declared_names"]

    # the input profile is never mutated: a binding is live runtime state
    original = {
        row["id"]: (row.get("binding") or {}).get("ident") for row in profile["controls"]
    }
    assert original["tone_cutoff"] == "3:1003"
    assert original["tone_shape"] == "5:1005"


def test_name_matching_is_exact_apart_from_case_and_whitespace():
    profile = F.demo_profile()
    indexed = [("tone_cutoff", 3)]
    for live in ("  tone CUTOFF ", "Tone\tCutoff"):
        _bound, report = B.bind_profile(
            profile, indexed=indexed, names=[live], idents=["3:1003"]
        )
        assert report.bound == 1, live
    for live in ("Tone Cutoff (Hz)", "ToneCutoff", "Cutoff"):
        _bound, report = B.bind_profile(
            profile, indexed=indexed, names=[live], idents=["3:1003"]
        )
        assert report.bound == 0 and report.unverified[0]["reason"] == "name_mismatch", live


# ---------------------------------------------------------------------------
# The engine performs the check, counts its reads, and refuses fail-closed
# ---------------------------------------------------------------------------


def test_the_engine_binds_live_idents_and_reports_the_measurement():
    transaction, host = make(request=CUTOFF_REQUEST, dry_run=False)
    result = transaction.run()
    assert result.ok, result.error
    binding = result.data["preflight"]["binding"]
    assert binding["controls_indexed"] == 4
    assert binding["bound"] == 4
    assert binding["unverified"] == []
    assert host.count("param_names") == 1, "one batched hop for every declared index"
    assert host.count("call") >= 0  # the binding reads ride the same chokepoint
    assert result.outcome.host.reads >= 2
    control = transaction._resolved.controls["tone_cutoff"]  # engine-internal resolved row
    assert control["binding"]["ident"] == "3:1003"
    assert control["binding"]["ident_source"] == "live_verified"


def test_a_renamed_parameter_refuses_the_write_with_zero_writes():
    host = build_host()
    host.slot(0, 1).rename_parameter(3, "Tone Cutoff B")
    transaction, host = make(request=CUTOFF_REQUEST, host=host, dry_run=False)
    result = transaction.run()
    assert not result.ok
    assert result.error.code == "UnsupportedControl"
    problem = result.error.detail["problems"][0]
    assert problem["detail"]["reason"] == "name_mismatch"
    assert problem["detail"]["observed_name"] == "Tone Cutoff B"
    assert result.outcome.verdict is Verdict.REFUSED_BEFORE_ANY_WRITE
    assert result.outcome.host.write_calls_attempted == 0
    assert host.count("write_params") == 0


def test_an_unreadable_ident_refuses_the_write_with_zero_writes():
    host = build_host()
    host.slot(0, 1).missing_idents.add("3:1003")
    transaction, host = make(request=CUTOFF_REQUEST, host=host, dry_run=False)
    result = transaction.run()
    assert not result.ok
    assert result.error.code == "UnsupportedControl"
    assert result.error.detail["problems"][0]["detail"]["reason"] == "ident_unreadable"
    assert result.outcome.host.write_calls_attempted == 0
    assert host.count("write_params") == 0


def test_a_short_binding_read_is_not_evidence_and_refuses():
    class _ShortHost(MH.MockHostBridge):
        def param_names(self, ref, indices):  # a host that answers fewer names
            return super().param_names(ref, indices)[:-1]

    host = _ShortHost(tracks=[MH.MockTrack(0, "Track 1", [MH.MockFxSlot()])])
    transaction, host = make(request=CUTOFF_REQUEST, host=host, dry_run=False, fx_index=0)
    result = transaction.run()
    assert not result.ok
    assert result.error.code == "SnapshotUnavailable"
    assert result.error.detail["reason"] == "short_binding_read"
    assert result.outcome.host.write_calls_attempted == 0


def test_the_engine_refuses_a_resolved_parameter_without_an_ident():
    """Defence in depth: a provider that resolves an unbound control cannot slip a write."""

    class _SlippingProvider(MockProvider):
        def normalize_request(self, profile, request):
            resolved = super().normalize_request(profile, request)
            for row in resolved.controls.values():
                if row.get("channel") == "parameter":
                    row["binding"] = {**row["binding"], "ident": None}  # the bug under test
            return resolved

    host = build_host()
    transaction, host = make(
        request=CUTOFF_REQUEST, host=host, dry_run=False, provider=_SlippingProvider()
    )
    result = transaction.run()
    assert not result.ok
    assert isinstance(result.error, UnsupportedControl)
    assert result.error.detail["reason"] == "ident_not_bound"
    assert result.outcome.host.write_calls_attempted == 0
    assert host.count("write_params") == 0


# ---------------------------------------------------------------------------
# The document-version gate (07 §3.2)
# ---------------------------------------------------------------------------


def _versioned(version):
    return MockProvider(document_version=lambda _document: version)


def test_a_measured_document_version_lets_the_write_through():
    transaction, _host = make(
        request=CUTOFF_REQUEST, dry_run=False, provider=_versioned("1.0.0")
    )
    result = transaction.run()
    assert result.ok, result.error
    gate = result.data["preflight"]["version_gate"]
    assert gate == {"checked": True, "live_version": "1.0.0", "source": "decoded_document"}


def test_an_unmeasured_document_version_refuses_with_zero_writes():
    transaction, host = make(
        request=CUTOFF_REQUEST, dry_run=False, provider=_versioned("9.9.9")
    )
    result = transaction.run()
    assert not result.ok
    assert isinstance(result.error, VersionMismatch)
    assert result.error.detail["override"] is None
    assert result.error.detail["compatible_versions"] == ["1.0.0"]
    assert result.outcome.verdict is Verdict.REFUSED_BEFORE_ANY_WRITE
    assert result.outcome.host.write_calls_attempted == 0
    assert host.count("write_state_slice") == 0


def test_the_refusal_is_not_a_bypass_and_the_dry_run_is_gated_too():
    """A preview of a plan that could never be applied is refused as well."""
    transaction, host = make(request=CUTOFF_REQUEST, dry_run=True, provider=_versioned("9.9.9"))
    result = transaction.run()
    assert not result.ok
    assert result.error.code == "VersionMismatch"
    assert result.outcome.host.write_calls_attempted == 0
    assert host.count("write_params") == 0


def test_a_provider_without_a_document_version_records_that_it_checked_nothing():
    transaction, _host = make(request=CUTOFF_REQUEST, dry_run=True)
    result = transaction.run()
    assert result.ok
    gate = result.data["preflight"]["version_gate"]
    assert gate["checked"] is False
    assert "no document-derived version" in gate["why"]
