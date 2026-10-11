"""P11 — the release-review policies (owner Decisions 3 and 4), 2026-10-10.

Decision 4 — `force`: V1 implements **no override**. `force=True` is a typed
refusal (`UnsupportedOverride`) before any host work, for every modulation op and
at both boundaries (tool surface and Core), and a wrong type for the argument is
refused rather than coerced. A hard dependency conflict is therefore never
bypassable through `force`.

Decision 3 — instance identity: the target token validates **location, build
facts and the state revision**, and it explicitly does **not** claim instance
continuity, because the L-3 measurement showed an identical-build replacement is
indistinguishable (same slot, same handle, same build facts). What is decidable
is enforced (a caller-supplied identity or token that disagrees with the host is
a typed refusal before any write); what is not decidable is disclosed, never
claimed.

These tests are mock/static only: no live host, no REAPER.
"""

from __future__ import annotations

import json

import pytest

import fixtures as F
from mock_provider import MockProvider

from reaper_reapy_mcp.plugins import model as M
from reaper_reapy_mcp.plugins import resolve as RS
from reaper_reapy_mcp.plugins import tools as T
from reaper_reapy_mcp.plugins.errors import TargetChanged, UnsupportedOverride

# the P9 harness (mock host + demo provider + a registered FastMCP surface)
from test_p9_tools import build_host, call, demo  # noqa: F401

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

INSTANCE = {"track_index": 0, "fx_index": 1}
WRITE_VERBS = ("write_params", "write_state_slice")


def write_verbs(host) -> list[str]:
    return [label for label, _detail in host.log if label in WRITE_VERBS]


def ref_of(host, *, fx_index: int = 1) -> dict:
    facts = host.fx_facts(0, fx_index)
    return {
        "host": "reaper",
        "location": {"track_index": 0, "fx_index": fx_index},
        "provider": "demo",
        "identity": RS.facts_identity(facts),
    }


# ---------------------------------------------------------------------------
# Decision 4 — force is refused, never honoured
# ---------------------------------------------------------------------------


def test_force_true_is_a_typed_refusal_at_the_registered_tool_boundary(demo):
    envelope = call(
        demo["mcp"],
        "plugin_modulation",
        {
            "instance": INSTANCE,
            "op": "create",
            "routes": [{"source": "src_a", "destination": "tone_level", "amount": 0.5}],
            "force": True,
        },
    )
    assert envelope["ok"] is False, envelope
    assert envelope["error"]["code"] == "UnsupportedOverride"
    assert envelope["error"]["detail"]["argument"] == "force"
    assert envelope["error"]["detail"]["supported"] is False
    assert envelope["error"]["detail"]["reason"] == "force_not_supported"
    assert envelope["error"]["detail"]["op"] == "create"
    # refused before any host work: not even a read of the instance state
    assert demo["host"].count("read_state_chunk") == 0
    assert demo["host"].count("fx_facts") == 0
    assert write_verbs(demo["host"]) == []
    assert json.dumps(envelope), "the typed refusal must serialize"


def test_force_true_does_not_apply_even_with_dry_run_false(demo):
    before = dict(demo["host"].slot(0, 1).params)
    envelope = call(
        demo["mcp"],
        "plugin_modulation",
        {
            "instance": INSTANCE,
            "op": "create",
            "routes": [{"source": "src_a", "destination": "tone_level", "amount": 0.5}],
            "force": True,
            "dry_run": False,
        },
    )
    assert envelope["ok"] is False and envelope["error"]["code"] == "UnsupportedOverride"
    assert demo["host"].slot(0, 1).params == before
    assert write_verbs(demo["host"]) == []


def test_force_true_is_refused_for_every_op_including_read_only_ones(demo):
    for op in ("list", "depth", "create", "update", "remove"):
        envelope = call(
            demo["mcp"],
            "plugin_modulation",
            {"instance": INSTANCE, "op": op, "force": True},
        )
        assert envelope["ok"] is False, op
        assert envelope["error"]["code"] == "UnsupportedOverride", op
    assert demo["host"].count("read_state_chunk") == 0
    assert write_verbs(demo["host"]) == []


@pytest.mark.parametrize("value", [1, "false", "true", "0", [], {}])
def test_a_wrong_type_for_force_is_refused_rather_than_coerced(demo, value):
    """``bool("false")`` is True: a falsy-looking string must never opt a caller
    into a force path, and a truthy string must not reach one either."""
    envelope = call(
        demo["mcp"],
        "plugin_modulation",
        {
            "instance": INSTANCE,
            "op": "create",
            "routes": [{"source": "src_a", "destination": "tone_level"}],
            "force": value,
        },
    )
    assert envelope["ok"] is False, value
    assert envelope["error"]["code"] == "InvalidValue"
    assert envelope["error"]["detail"]["argument"] == "force"
    assert envelope["error"]["detail"]["value"] == value
    assert write_verbs(demo["host"]) == []


def test_force_false_is_accepted_and_changes_nothing(demo):
    envelope = call(
        demo["mcp"],
        "plugin_modulation",
        {"instance": INSTANCE, "op": "list", "force": False},
    )
    assert envelope["ok"] is True, envelope
    assert envelope["meta"]["dry_run"] is True


def test_a_hard_dependency_conflict_cannot_be_bypassed_with_force(demo):
    """The rule fires on `settings.mode` while a route is active (fixtures.py).

    With the conflict present the request is refused by the rule
    (`DependencyViolation`, zero writes); adding `force` does not turn it into a
    success - it produces the override refusal instead. Both end with zero
    writes, and neither reaches a write verb.
    """
    request = {"state": {"settings.mode": "mono"}}
    conflict = call(demo["mcp"], "plugin_write", {"instance": INSTANCE, "request": request, "dry_run": False})
    assert conflict["ok"] is False, conflict
    assert conflict["error"]["code"] == "DependencyViolation"
    assert conflict["outcome"]["verdict"] == "REFUSED_BEFORE_ANY_WRITE"
    assert conflict["outcome"]["host"]["write_calls_attempted"] == 0
    assert write_verbs(demo["host"]) == []

    # `plugin_write` does not even declare `force`; a caller trying it through
    # the modulation surface still cannot get the write applied
    reads_before = demo["host"].count("read_state_chunk")
    bypass = call(
        demo["mcp"],
        "plugin_modulation",
        {
            "instance": INSTANCE,
            "op": "create",
            "routes": [{"source": "src_a", "destination": "tone_level"}],
            "force": True,
            "dry_run": False,
        },
    )
    assert bypass["ok"] is False and bypass["error"]["code"] == "UnsupportedOverride"
    assert write_verbs(demo["host"]) == []
    assert demo["host"].count("read_state_chunk") == reads_before, (
        "the override refusal must precede any host contact"
    )


def test_the_declared_schema_documents_that_force_is_not_supported():
    force_arg = T.SPEC["plugin_modulation"]["args"]["force"]
    assert force_arg["default"] is False
    assert "not supported" in force_arg["note"].lower()
    description = T.synthesize_docstring("plugin_modulation")
    assert "not supported" in description.lower()


def test_the_core_also_refuses_force_without_a_tool_in_front_of_it():
    """Defence in depth: the Core is callable without the tool closure."""
    from reaper_reapy_mcp.plugins import modulation as MOD
    from reaper_reapy_mcp.plugins.model import PluginInstanceRef
    from reaper_reapy_mcp.plugins.resolve import TargetTokenRegistry

    host = build_host()
    provider = MockProvider()
    facts = host.fx_facts(0, 1)
    registry = TargetTokenRegistry(session_context={"project": "test"})
    with pytest.raises(UnsupportedOverride):
        MOD.modulation(
            provider=provider,
            bridge=host,
            profile=provider.build_profile(provider.identify(facts)),
            ref=PluginInstanceRef(
                host="reaper", location={"track_index": 0, "fx_index": 1}, provider="demo", identity={}
            ),
            token=registry.issue(facts=facts),
            registry=registry,
            op="create",
            routes=[{"source": "src_a", "destination": "tone_level"}],
            force=True,
        )
    assert write_verbs(host) == []


# ---------------------------------------------------------------------------
# Decision 3 — identity claims: what is checked, what is disclosed
# ---------------------------------------------------------------------------


def test_a_caller_supplied_identity_that_disagrees_with_the_host_refuses(demo):
    instance = {
        "location": {"track_index": 0, "fx_index": 1},
        "provider": "demo",
        "identity": {"fx_name": F.DEMO_NAME, "class_id": F.FOREIGN_CLASS, "module": F.DEMO_MODULE},
    }
    envelope = call(
        demo["mcp"],
        "plugin_write",
        {"instance": instance, "request": {"controls": {"tone_cutoff": 24}}, "dry_run": False},
    )
    assert envelope["ok"] is False, envelope
    assert envelope["error"]["code"] == "TargetChanged"
    assert envelope["error"]["detail"]["reason"] == "identity_mismatch"
    assert "class_id" in envelope["error"]["detail"]["fields"]
    assert write_verbs(demo["host"]) == []


def test_a_matching_caller_identity_is_accepted(demo):
    envelope = call(
        demo["mcp"],
        "plugin_write",
        {"instance": ref_of(demo["host"]), "request": {"controls": {"tone_cutoff": 24}}},
    )
    assert envelope["ok"] is True, envelope
    assert envelope["data"]["dry_run"] is True
    assert envelope["outcome"]["verdict"] == "NO_WRITES"


def test_an_unknown_target_token_is_refused_not_ignored(demo):
    """Before this check a token the layer could not honour was silently unused."""
    instance = dict(INSTANCE)
    instance["target_token"] = "tok_" + "0" * 32
    envelope = call(
        demo["mcp"],
        "plugin_write",
        {"instance": instance, "request": {"controls": {"tone_cutoff": 24}}, "dry_run": False},
    )
    assert envelope["ok"] is False, envelope
    assert envelope["error"]["code"] == "TargetChanged"
    assert envelope["error"]["detail"]["reason"] == "unknown_target_token"
    assert write_verbs(demo["host"]) == []


def test_a_token_is_refused_for_a_mutation_and_accepted_for_a_read(demo):
    """Safety-gate correction (2026-10-11): a token is a continuity claim.

    The token asserts "the instance that was validated", and the measured host
    cannot back that (an identical-build replacement is indistinguishable), so a
    **mutation** carrying one is refused before any host work - accepting it would
    let the caller's belief stand in for verification. A **read** makes no write
    claim, so the same token is still accepted there; the remedy for a mutation is
    to address the instance by location, which is a present-tense claim the layer
    verifies inside the call.
    """
    issued = call(demo["mcp"], "plugin_instances", {})
    token = issued["meta"]["target_token"]
    instance = dict(INSTANCE)
    instance["target_token"] = token

    # a read keeps working with the token
    read = call(demo["mcp"], "plugin_read", {"instance": instance, "controls": ["tone_cutoff"]})
    assert read["ok"] is True, read

    # a mutation does not, and the refusal happens before any host contact
    before = demo["host"].count("fx_facts")
    for tool, arguments in (
        ("plugin_write", {"request": {"controls": {"tone_cutoff": 24}}, "dry_run": False}),
        ("plugin_write", {"request": {"controls": {"tone_cutoff": 24}}}),
        ("plugin_modulation", {"op": "create", "routes": [{"source": "src_a", "destination": "tone_level"}]}),
        ("plugin_modulation", {"op": "remove", "selector": {"slot": 1}, "dry_run": False}),
    ):
        envelope = call(demo["mcp"], tool, {"instance": instance, **arguments})
        assert envelope["ok"] is False, (tool, arguments, envelope)
        assert envelope["error"]["code"] == "IdentityContinuityUnprovable", envelope["error"]
        assert envelope["error"]["detail"]["reason"] == "instance_continuity_not_provable"
        assert envelope["error"]["detail"]["target_token"] == token[:8] + "…"
        assert "remedy" in envelope["error"]["detail"]
    assert demo["host"].count("fx_facts") == before, "the refusal precedes any host call"
    assert write_verbs(demo["host"]) == []

    # the read-only op with the token is still fine
    listed = call(demo["mcp"], "plugin_modulation", {"instance": instance, "op": "list"})
    assert listed["ok"] is True, listed


def test_every_plugin_response_discloses_what_the_token_does_not_claim(demo):
    for tool, arguments in (
        ("plugin_instances", {}),
        ("plugin_describe", {"instance": INSTANCE}),
        ("plugin_find", {"instance": INSTANCE, "query": "tone"}),
        ("plugin_read", {"instance": INSTANCE, "controls": ["tone_cutoff"]}),
        ("plugin_write", {"instance": INSTANCE, "request": {"controls": {"tone_cutoff": 24}}}),
        ("plugin_state", {"instance": INSTANCE}),
        ("plugin_modulation", {"instance": INSTANCE, "op": "list"}),
    ):
        envelope = call(demo["mcp"], tool, arguments)
        assert envelope["ok"] is True, (tool, envelope)
        basis = envelope["meta"]["identity_basis"]
        assert basis["not_claimed"] == ["instance_continuity"], tool
        assert basis["reason"] == "identical_build_replacement_indistinguishable", tool
        assert "location" in basis["validated"] and "state_revision" in basis["validated"], tool
        assert "p8_l3_identity" in basis["measured_by"], tool
        assert "does not claim" in basis["consequence"] or "not claim" in basis["consequence"], tool


def test_refusal_envelopes_carry_the_disclosure_too(demo):
    """A refusal raised before a target exists has no token, but it must still say
    what the layer does not claim about identity (audit finding, 2026-10-10)."""
    refusals = [
        ("plugin_modulation", {"instance": INSTANCE, "op": "list", "force": True}),
        ("plugin_write", {"instance": {"location": {"track_index": 0, "fx_index": 1}, "identity": {"class_id": F.FOREIGN_CLASS}}}),
        ("plugin_write", {}),
    ]
    for tool, arguments in refusals:
        envelope = call(demo["mcp"], tool, arguments)
        assert envelope["ok"] is False, (tool, arguments)
        assert envelope["meta"]["identity_basis"]["not_claimed"] == ["instance_continuity"], tool


def test_an_identity_claim_that_cannot_be_compared_is_refused(demo):
    """Silently ignoring an uncomparable claim is how a caller believes it was checked."""
    for supplied in ("same-as-before", {"bogus": 1}, [1, 2]):
        envelope = call(
            demo["mcp"],
            "plugin_write",
            {
                "instance": {"location": {"track_index": 0, "fx_index": 1}, "identity": supplied},
                "request": {"controls": {"tone_cutoff": 24}},
                "dry_run": False,
            },
        )
        assert envelope["ok"] is False, supplied
        assert envelope["error"]["code"] == "InvalidValue"
        assert envelope["error"]["detail"]["reason"] == "identity_claim_not_comparable"
        assert write_verbs(demo["host"]) == []


def test_the_token_alias_is_honoured_as_a_claim(demo):
    """`instance.token` is read the same way as `instance.target_token`."""
    envelope = call(
        demo["mcp"],
        "plugin_write",
        {
            "instance": {"location": {"track_index": 0, "fx_index": 1}, "token": "tok_" + "0" * 32},
            "request": {"controls": {"tone_cutoff": 24}},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is False, envelope
    assert envelope["error"]["code"] == "TargetChanged"
    assert envelope["error"]["detail"]["reason"] == "unknown_target_token"
    assert write_verbs(demo["host"]) == []


def test_the_disclosure_names_the_measurement_that_limits_it():
    basis = RS.identity_basis()
    assert basis["not_claimed"] == ["instance_continuity"]
    assert basis["measured_by"].endswith("p8_l3_identity__20261010T203101Z__dcd339e9c219.json")
    # a copy, so a caller cannot mutate the module constant through the response
    basis["not_claimed"].append("tampered")
    assert RS.identity_basis()["not_claimed"] == ["instance_continuity"]


def test_identical_build_replacement_with_a_different_build_refuses_before_any_write():
    """The decidable half of L-3, up to the write verb.

    A token issued for a validated instance is revalidated against fresh facts
    before every write unit. The fixture repopulates the same slot with a
    *different* build (the host's facts move), which must refuse with
    `write_calls_attempted == 0` - the layer never silently re-targets.
    """
    from reaper_reapy_mcp.plugins.transaction import Transaction

    host = build_host()
    provider = MockProvider()
    facts = host.fx_facts(0, 1)
    registry = RS.TargetTokenRegistry(session_context={"project": "test"})
    token = registry.issue(facts=facts)
    ref = M.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 1}, provider="demo", identity={}
    )
    host.slot(0, 1).replace_identically(class_id=F.FOREIGN_CLASS, name=F.FOREIGN_NAME)
    result = Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=provider.build_profile(provider.identify(facts)),
        ref=ref,
        request={"controls": {"tone_cutoff": 24.0}},
        token=token,
        registry=registry,
        dry_run=False,
    ).run()
    assert result.ok is False
    assert result.error is not None and result.error.code == "TargetChanged"
    assert result.error.detail["reason"] == "target_facts_changed"
    assert "class_id" in result.error.detail["fields"]
    assert result.outcome.verdict is M.Verdict.REFUSED_BEFORE_ANY_WRITE
    assert result.outcome.host.write_calls_attempted == 0
    assert write_verbs(host) == []


def test_identical_build_replacement_with_identical_facts_is_allowed_and_disclosed():
    """The undecidable half of L-3, stated honestly instead of claimed.

    Same slot, same build facts, same handle (the measured live behaviour): the
    layer cannot prove the instance is the one a previous call addressed, so it
    does **not** claim it - the write proceeds against the instance that is there
    now, verified and restorable, and the response says exactly that.
    """
    from reaper_reapy_mcp.plugins.transaction import Transaction

    host = build_host()
    provider = MockProvider()
    facts = host.fx_facts(0, 1)
    registry = RS.TargetTokenRegistry(session_context={"project": "test"})
    token = registry.issue(facts=facts)
    ref = M.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 1}, provider="demo", identity={}
    )
    host.slot(0, 1).replace_identically()  # an identical-build replacement: no observable difference
    result = Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=provider.build_profile(provider.identify(facts)),
        ref=ref,
        request={"controls": {"tone_cutoff": 24.0}},
        token=token,
        registry=registry,
        dry_run=False,
    ).run()
    assert result.ok is True, result.error
    assert result.outcome.verdict is M.Verdict.ALL_VERIFIED
    assert result.outcome.host.write_calls_attempted >= 1
    # the outcome carries no identity claim at all - the limits live in meta
    assert "identity" not in json.dumps(result.outcome.to_dict())
    assert RS.identity_basis()["not_claimed"] == ["instance_continuity"]


def test_the_token_refusal_reasons_are_machine_readable():
    host = build_host()
    registry = RS.TargetTokenRegistry(session_context={"project": "test"})
    host.slot(0, 1).session_handle = "handle-before"
    facts = host.fx_facts(0, 1)
    with pytest.raises(TargetChanged) as unknown:
        registry.get("tok_missing")
    assert unknown.value.detail["reason"] == "unknown_target_token"
    token = registry.issue(facts=facts)
    host.slot(0, 1).replace_identically(session_handle="handle-after")
    with pytest.raises(TargetChanged) as changed:
        registry.revalidate(token.id, host.fx_facts(0, 1))
    assert changed.value.detail["reason"] == "target_facts_changed"
    assert "session_handle" in changed.value.detail["fields"]
