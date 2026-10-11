"""P20 — `routes_update` converts a semantic `depth` (defect D1, 2026-10-11, live).

**Measured on the live instance.** `routes_create` converts a semantic depth
through `provider.convert_depth`; `routes_update` did not. An update of an
*existing* route therefore arrived at the state codec carrying `depth`, which is
not a route field there (`source`/`destination`/`amount`/`bipolar`/`power`/... are),
and was refused as "*an update names no field to change*" - so the documented
example `{"slot": 3, "depth": -6}` (06 §8 and §9) could not work at all, and the
error it produced described the wrong problem. Found by probe A when a diagnostic
arm tried to set the pitch route's depth to 0 (arm A1).

**The fix** converts the depth in `normalize_request`, with the same hook
`routes_create` uses. The conversion needs the **destination**; for an existing
route that lives in the live table, and normalization performs no host access
(by design), so an update that names only a depth is refused with *that* reason
and its slot - never with the codec's misleading one, and never silently. An
update that carries both an `amount` and a `depth` keeps the amount and drops the
depth, so the codec never receives a field it does not know.
"""

from __future__ import annotations

import pytest

from reaper_reapy_mcp.plugins.providers.vital import DEFAULT_REGISTRY_DIR, VitalProvider, load_registry
from test_p7_vital import IDENTITY  # noqa: F401 - the fixture identity the suite shares


@pytest.fixture(scope="module")
def provider() -> VitalProvider:
    try:
        registry = load_registry(directory=str(DEFAULT_REGISTRY_DIR))
    except Exception as exc:  # noqa: BLE001 - an absent registry is a skip, as in P16/P19
        pytest.skip(f"the measured registry is not present: {exc}")
    return VitalProvider(registry=registry)


@pytest.fixture(scope="module")
def profile(provider: VitalProvider) -> dict:
    return provider.build_profile(dict(IDENTITY))


def test_an_update_with_a_named_destination_converts_exactly_like_create(provider, profile):
    created = provider.normalize_request(
        profile, {"routes_create": [{"source": "env_2", "destination": "osc_1_transpose", "depth": 12.0}]}
    )
    assert created.problems == [], created.problems
    assert len(created.routes_create) == 1, created.routes_create
    amount_from_create = float(created.routes_create[0]["amount"])

    updated = provider.normalize_request(
        profile, {"routes_update": [{"slot": 1, "destination": "osc_1_transpose", "depth": 12.0}]}
    )
    assert updated.problems == [], updated.problems
    assert len(updated.routes_update) == 1, updated.routes_update
    assert float(updated.routes_update[0]["amount"]) == pytest.approx(amount_from_create, rel=1e-9)
    assert "depth" not in updated.routes_update[0], updated.routes_update[0]


@pytest.mark.parametrize("depth", [7.0, 12.0, 18.0, -6.0])
def test_the_update_amount_is_the_providers_own_conversion(provider, profile, depth):
    expected = float(provider.convert_depth(profile, "osc_1_transpose", depth))
    resolved = provider.normalize_request(
        profile, {"routes_update": [{"slot": 1, "destination": "osc_1_transpose", "depth": depth}]}
    )
    assert resolved.problems == [], resolved.problems
    assert float(resolved.routes_update[0]["amount"]) == pytest.approx(expected, rel=1e-9)


def test_an_update_without_a_destination_is_refused_with_that_reason(provider, profile):
    resolved = provider.normalize_request(profile, {"routes_update": [{"slot": 1, "depth": 12.0}]})
    assert resolved.routes_update == [], resolved.routes_update
    assert len(resolved.problems) == 1, resolved.problems
    problem = resolved.problems[0]
    assert problem["code"] == "InvalidValue", problem
    assert problem["detail"]["reason"] == "depth_without_destination", problem
    assert problem["detail"]["slot"] == 1, problem
    # the refusal is about the missing input, not about the value
    assert "destination" in problem["message"], problem["message"]


def test_an_amount_and_a_depth_together_keep_the_amount(provider, profile):
    resolved = provider.normalize_request(
        profile, {"routes_update": [{"slot": 2, "amount": 0.5, "depth": 12.0, "bipolar": 1.0}]}
    )
    assert resolved.problems == [], resolved.problems
    entry = resolved.routes_update[0]
    assert entry["amount"] == pytest.approx(0.5), entry
    assert entry["bipolar"] == pytest.approx(1.0), entry
    assert "depth" not in entry, entry


def test_an_update_with_an_amount_is_untouched(provider, profile):
    resolved = provider.normalize_request(profile, {"routes_update": [{"slot": 3, "amount": 0.25}]})
    assert resolved.problems == [], resolved.problems
    assert resolved.routes_update == [{"slot": 3, "amount": 0.25}], resolved.routes_update
