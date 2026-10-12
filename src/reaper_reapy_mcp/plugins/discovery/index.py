"""A reusable, targeted index over ONE prepared discovery artifact (P28).

Why this module exists (the P27 measurement): a prepared artifact for a real
plugin holds thousands of controls (the round's two synths both report 2858),
and a musical request needs a handful of them. Two failures followed from
answering such a request without an index:

* the model was handed the whole control list (the artifact's own compact view
  with ``include_controls`` measured 28 MB of JSON), or
* the request fell back to the classic name scan, which costs one host roundtrip
  **per parameter** (`TrackFX_GetParamName`): measured live 2026-10-12 at
  **31.4 ms/param** unheld = **89.7 s** for one pass over 2858 parameters, and
  the classic setter makes up to two passes.

An artifact is *data*: it is read once, and every lookup below is built from it
in one pass. Nothing here touches the host, and nothing here re-derives what the
artifact already measured - the index is a **view**, and a view that disagreed
with its artifact would be worse than no view at all.

Invariants:

* one index per (path, content digest), built at most once per process for a
  given artifact (``load_index``); a *different* digest is a different index;
* `find` never returns more than ``limit`` rows, and always reports how many
  controls matched in total, so a caller can tell "12 of 12" from "12 of 340";
* a term that matches nothing is reported as such (``unmatched_terms``) with
  suggestions - a search that silently ignores a word would let a model believe
  it searched for something it did not;
* every match carries *why* it matched, including when the reason is a synonym
  rather than the plugin's own spelling. Heuristic relevance is labelled as
  heuristic (brief §6.3: "do not hide the uncertainty").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "ControlIndex",
    "IndexedControl",
    "ROLE_SYNONYMS",
    "load_index",
    "index_census",
]

#: Words a musician uses that the plugin does not spell the same way. Each key is
#: the word a caller is likely to type; the values are the tokens that appear in
#: real parameter names (measured on the two synths this layer was verified
#: against: "A Osc 1 Pitch", "A Amp EG Attack", "A Filter 1 Cutoff",
#: "A Waveshaper Drive", ...). The table is data, and a match through it is
#: reported as ``role`` - never as if the plugin had used the word.
ROLE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "oscillator": ("osc", "vco", "operator"),
    "pitch": ("pitch", "tune", "transpose", "octave", "semitones", "keytrack"),
    "envelope": ("eg", "env", "envelope", "adsr", "attack", "decay", "sustain", "release"),
    "attack": ("attack", "atk"),
    "decay": ("decay", "dcy"),
    "sustain": ("sustain", "sus"),
    "release": ("release", "rel"),
    "filter": ("filter", "cutoff", "resonance", "lowpass", "highpass", "bandpass", "lp", "hp"),
    "cutoff": ("cutoff", "freq", "frequency", "hz"),
    "resonance": ("resonance", "reso", "q"),
    "distortion": (
        "distortion",
        "waveshaper",
        "drive",
        "saturat",
        "overdrive",
        "clip",
        "shaper",
        "fold",
    ),
    "level": ("level", "volume", "gain", "amp", "amplitude", "mix", "amount"),
    "pan": ("pan", "balance"),
    "noise": ("noise",),
    "unison": ("unison", "voices", "detune"),
    "portamento": ("portamento", "glide", "legato"),
    "lfo": ("lfo", "low frequency", "modulator"),
    "delay": ("delay", "echo"),
    "reverb": ("reverb", "wet", "room", "decay time"),
    "chorus": ("chorus", "ensemble", "flanger", "phaser"),
    "mute": ("mute", "bypass", "enable"),
    "modulation": ("mod", "modulation", "matrix", "route", "source", "target"),
    "waveform": ("wave", "shape", "type", "formant"),
    "octave": ("octave", "transpose"),
}

_SEPARATORS = re.compile(r"[^0-9a-z]+")


def normalise(text: Any) -> str:
    """Lower-case, separator-collapsed text (the one normalisation used here)."""
    if text is None:
        return ""
    return _SEPARATORS.sub(" ", str(text).strip().lower()).strip()


def tokens_of(text: Any) -> tuple[str, ...]:
    normalised = normalise(text)
    return tuple(token for token in normalised.split(" ") if token)


class _TokenSet(tuple):
    """A token tuple with O(1) membership (used for every control's tokens)."""

    def __new__(cls, tokens: Iterable[str]) -> "_TokenSet":
        return super().__new__(cls, tokens)

    def __init__(self, tokens: Iterable[str]) -> None:  # noqa: D107 - tuple subclass
        self._set = set(self)

    def __contains__(self, item: object) -> bool:  # type: ignore[override]
        return item in self._set


def role_tokens(token: str) -> tuple[str, ...]:
    """The tokens a role word expands to (the word itself when it is not one)."""
    return ROLE_SYNONYMS.get(token, (token,))


def duplicated_bases(bases: Iterable[str]) -> set[str]:
    """Which base ids occur more than once (compared normalised)."""
    counts: dict[str, int] = {}
    for base in bases:
        key = normalise(base)
        counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count > 1}


def disambiguated(base: str, index: int, duplicated: set[str]) -> str:
    """`base`, or `base #index` when that base names more than one control.

    One rule, used by both the index and the materializer: a plugin really can
    name two controls the same (ReaComp has two `Attack`s, measured live in P28
    stage G), and an id that names two targets is a wrong-write hazard.
    """
    return f"{base} #{index}" if normalise(base) in duplicated else base


@dataclass(frozen=True)
class IndexedControl:
    """One control of the artifact, in the shape the tools answer with."""

    index: int
    name: str
    ident: str | None
    label: str
    module: str | None
    klass: str
    confidence: str
    unit: str | None
    range: tuple[float, float] | None
    law_kind: str | None
    law_accepted: bool | None
    step: float | None
    n_members: int
    discrete_family: str | None
    reasons: tuple[str, ...] = ()
    tokens: _TokenSet = field(default_factory=lambda: _TokenSet(()))
    module_tokens: _TokenSet = field(default_factory=lambda: _TokenSet(()))
    roles: tuple[str, ...] = ()
    #: The **labels the host printed** for this control's states, in order.
    #: A count says "this is a chooser"; the labels are what a caller needs to
    #: choose one - and on the discovery path they come from the artifact's own
    #: samples (measured), never from a guess about what member N means.
    enum_labels: tuple[str, ...] = ()
    #: set only when this control's own label/name is shared with another one
    id_override: str | None = None

    @property
    def base_id(self) -> str:
        """The id this control's own label/name gives it, before disambiguation."""
        return self.label or self.name or f"#{self.index}"

    @property
    def control_id(self) -> str:
        """The id a caller uses to address this control (stable per artifact).

        Unique by construction: a plugin can name two controls the same (measured
        live on ReaComp, which has two `Attack` controls - P28 stage G), and an id
        that names two targets is a wrong-write hazard, so a collision is resolved
        deterministically here (`label #index`) rather than by whichever control
        happened to be indexed last.
        """
        if self.id_override:
            return self.id_override
        return self.label or self.name or f"#{self.index}"

    def compact(self, *, why: Sequence[str] = (), matched: Sequence[str] = ()) -> dict[str, Any]:
        """The row a `plugin_find` answer carries: enough to act on, nothing more.

        The shape is the **tool's**, not the artifact's: the same keys a
        provider-profile control row has, so a caller (and the model reading the
        answer) never has to know which path served it. `binding.index` is the
        address the write path uses, and `type` is what `values` dispatches on.
        """
        row: dict[str, Any] = {
            "kind": "control",
            "id": self.control_id,
            "label": self.name,
            "name": self.name,
            "index": self.index,
            "module": self.module,
            "type": "enum" if self.n_members else "number",
            "class": self.klass,
            "confidence": self.confidence,
            "binding": {"index": self.index, "ident": self.ident, "live_name": self.name},
        }
        if matched:
            row["matched_terms"] = list(matched)
        if why:
            row["why"] = list(why)
        if self.roles:
            row["roles"] = list(self.roles)
        if self.unit:
            row["unit"] = self.unit
        if self.range is not None:
            row["range"] = {"min": self.range[0], "max": self.range[1]}
        if self.law_kind:
            row["law"] = self.law_kind
        if self.n_members:
            row["members"] = self.n_members
        if self.enum_labels:
            # The words the host printed, in state order: a caller can address a
            # state by label (P29 - "Sine" has to be *chosen*, not inferred from
            # a member number).
            row["enum_labels"] = list(self.enum_labels)
        if self.step is not None:
            row["step"] = self.step
        return row


def _roles_of(tokens: Sequence[str]) -> tuple[str, ...]:
    token_set = set(tokens)
    roles = [
        role
        for role, members in ROLE_SYNONYMS.items()
        if token_set.intersection(members)
    ]
    return tuple(sorted(roles))


def _range_of(control: Mapping[str, Any]) -> tuple[float, float] | None:
    analysis = control.get("range")
    if not isinstance(analysis, Mapping):
        return None
    chosen = analysis.get("chosen")
    if not isinstance(chosen, (list, tuple)) or len(chosen) != 2:
        return None
    low, high = chosen
    if isinstance(low, bool) or isinstance(high, bool):
        return None
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    return (float(min(low, high)), float(max(low, high)))


def _label_of(control: Mapping[str, Any]) -> str:
    """The addressable id of a control: the raw host name, slugged.

    The host name is what the plugin itself prints ("A Filter 1 Cutoff"), so it
    is the one spelling a caller can match by eye and the one a read-back can be
    compared against. The slug form is used as the id because it survives being
    a JSON key in a request object.
    """
    name = str(control.get("name") or control.get("label") or "")
    return "_".join(tokens_of(name)) if name else f"param_{control.get('index')}"


class ControlIndex:
    """Every lookup a musical request needs, built in one pass over an artifact."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = dict(payload)
        self.identity: dict[str, Any] = dict(payload.get("identity") or {})
        self.counts: dict[str, Any] = dict(payload.get("counts") or {})
        self.cost: dict[str, Any] = dict(payload.get("cost") or {})
        self.mode = payload.get("mode")
        self.engine_version = payload.get("engine_version")
        controls: list[IndexedControl] = []
        self._modules: list[dict[str, Any]] = []
        for node in ((payload.get("modules") or {}).get("nodes") or []):
            self._modules.append(
                {
                    "id": node.get("id"),
                    "label": node.get("label"),
                    "kind": node.get("kind"),
                    "status": node.get("status"),
                    "confidence": node.get("confidence"),
                    "parent": node.get("parent"),
                    "signals": list(node.get("signals") or []),
                    "controls": [int(item) for item in (node.get("controls") or [])],
                }
            )
        module_label = {node["id"]: node.get("label") or node["id"] for node in self._modules}
        for row in payload.get("controls") or []:
            control = dict(row)
            classification = dict(control.get("classification") or {})
            mapping = control.get("mapping") or {}
            chosen = mapping.get("chosen") if isinstance(mapping, Mapping) else None
            discrete = dict(control.get("discrete") or {})
            steps = dict(control.get("steps") or {})
            module_id = control.get("module")
            label = _label_of(control)
            name = str(control.get("name") or label)
            module_text = module_label.get(module_id, module_id) or ""
            tokens = _TokenSet(tokens_of(name) + tokens_of(label) + tokens_of(control.get("ident")))
            controls.append(
                IndexedControl(
                    index=int(control.get("index") or 0),
                    name=name,
                    ident=control.get("ident"),
                    label=label,
                    module=module_id,
                    klass=str(classification.get("kind") or "unknown"),
                    confidence=str(classification.get("confidence") or "none"),
                    unit=control.get("unit"),
                    range=_range_of(control),
                    law_kind=(dict(chosen).get("kind") if isinstance(chosen, Mapping) else None),
                    law_accepted=(
                        bool(dict(chosen).get("accepted")) if isinstance(chosen, Mapping) else None
                    ),
                    step=(float(steps["step"]) if isinstance(steps.get("step"), (int, float)) else None),
                    n_members=len(discrete.get("members") or []),
                    enum_labels=tuple(
                        str(member.get("label"))[:48]
                        for member in (discrete.get("members") or [])[:32]
                        if member.get("label") not in (None, "")
                    ),
                    discrete_family=discrete.get("family"),
                    reasons=tuple(str(item) for item in (classification.get("reasons") or [])[:3]),
                    tokens=tokens,
                    module_tokens=_TokenSet(tokens_of(module_text) + tokens_of(module_id)),
                    roles=_roles_of(tokens),
                )
            )
        controls.sort(key=lambda item: item.index)
        # A duplicate label is not a reason to fail a search, and it is certainly
        # not a reason to let two controls share one id: the second one indexed
        # would silently win. Disambiguate deterministically and keep the tally.
        duplicates = duplicated_bases(control.base_id for control in controls)
        controls = [
            replace(control, id_override=disambiguated(control.base_id, control.index, duplicates))
            if normalise(control.base_id) in duplicates
            else control
            for control in controls
        ]
        self.id_collisions: tuple[str, ...] = tuple(sorted(duplicates))
        self.controls: list[IndexedControl] = controls
        self._by_index = {control.index: control for control in controls}
        self._by_id = {control.control_id: control for control in controls}
        self._by_name = {normalise(control.name): control for control in controls}
        self._by_ident = {
            control.ident: control for control in controls if control.ident
        }
        self._by_module: dict[str, list[IndexedControl]] = {}
        for control in controls:
            self._by_module.setdefault(str(control.module), []).append(control)
        vocab: set[str] = set()
        for control in controls:
            vocab.update(control.tokens)
            vocab.update(control.roles)
        self._vocabulary: tuple[str, ...] = tuple(sorted(vocab))

    # -- lookups -----------------------------------------------------------

    def census(self) -> dict[str, Any]:
        """What the index holds, per class and per module (counts only)."""
        per_class: dict[str, int] = {}
        per_confidence: dict[str, int] = {}
        for control in self.controls:
            per_class[control.klass] = per_class.get(control.klass, 0) + 1
            per_confidence[control.confidence] = per_confidence.get(control.confidence, 0) + 1
        return {
            "controls_indexed": len(self.controls),
            "classes": dict(sorted(per_class.items())),
            "class_confidence": dict(sorted(per_confidence.items())),
            "modules": len(self._modules),
            "with_law": sum(1 for control in self.controls if control.law_kind),
            "with_law_accepted": sum(1 for control in self.controls if control.law_accepted),
            "with_range": sum(1 for control in self.controls if control.range is not None),
            "discrete": sum(1 for control in self.controls if control.n_members),
            # A non-empty list means two controls share a LABEL: their ids carry a
            # `#index` suffix so each one is addressable (never a silent pick).
            "disambiguated_ids": list(getattr(self, "id_collisions", ())),
        }

    def nodes(self, *, top_level_only: bool = True, limit: int | None = None) -> list[dict[str, Any]]:
        rows = [
            {
                "id": node["id"],
                "label": node["label"],
                "kind": node["kind"],
                "status": node["status"],
                "confidence": node["confidence"],
                "parent": node["parent"],
                "n_controls": len(node["controls"]),
            }
            for node in self._modules
            if not top_level_only or node["parent"] is None
        ]
        return rows[:limit] if limit is not None else rows

    def control(self, address: Any) -> IndexedControl | None:
        """Resolve an id, a raw name, an ident, or ``#<index>``/an index."""
        if isinstance(address, bool):
            return None
        if isinstance(address, int):
            return self._by_index.get(address)
        text = str(address).strip()
        if text.startswith("#"):
            text = text[1:]
        if text.isdigit():
            found = self._by_index.get(int(text))
            if found is not None:
                return found
        for table in (self._by_id, self._by_name, self._by_ident):
            found = table.get(text) or table.get(normalise(text))
            if found is not None:
                return found
        wanted = normalise(text)
        for control in self.controls:
            if wanted and wanted == normalise(control.name):
                return control
        return None

    def module(self, module_id: Any) -> dict[str, Any] | None:
        wanted = str(module_id)
        node = next(
            (row for row in self._modules if str(row["id"]) == wanted or row["label"] == wanted),
            None,
        )
        if node is None:
            return None
        members = [
            control
            for control in self.controls
            if str(control.module) == str(node["id"]) or control.module in (None, "")
            and control.index in node["controls"]
        ]
        return {"node": dict(node), "controls": members}

    # -- search ------------------------------------------------------------

    def find(
        self,
        terms: Sequence[str],
        *,
        module: Any = None,
        kind: str | None = None,
        limit: int = 12,
        min_strength: int = 1,
    ) -> dict[str, Any]:
        """Rank the controls that answer ``terms``; bounded output, labelled reasons."""
        cleaned: list[str] = []
        for term in terms:
            raw = str(term).strip()
            text = normalise(raw)
            if raw.startswith("#") and text.isdigit():
                # `#12` is an address, not a word: the normaliser strips the `#`,
                # and losing it would turn a precise address into a fuzzy search.
                text = f"#{text}"
            if text and text not in cleaned:
                cleaned.append(text)
        limit = max(1, int(limit))
        kind = (kind or "").strip().lower() or None
        pool = self.controls
        if module:
            resolved = self.module(module)
            if resolved is None:
                raise KeyError(str(module))
            allowed = {control.index for control in resolved["controls"]}
            pool = [control for control in self.controls if control.index in allowed]
        if kind:
            pool = [
                control
                for control in pool
                if control.klass == kind or (control.discrete_family or "") == kind
            ]
        scored: list[tuple[tuple[int, int, int], IndexedControl, list[str], list[str]]] = []
        hit_terms: dict[str, int] = {term: 0 for term in cleaned}
        for control in pool:
            matched: list[str] = []
            why: list[str] = []
            strengths = 0
            for term in cleaned:
                hit = self._match(control, term)
                if hit is None:
                    continue
                strength, reason = hit
                matched.append(term)
                why.append(reason)
                strengths += strength
                hit_terms[term] += 1
            if len(matched) < min_strength or not matched:
                continue
            scored.append(((len(matched), strengths, -control.index), control, matched, why))
        scored.sort(key=lambda row: row[0], reverse=True)
        rows = [control.compact(why=why, matched=matched) for _s, control, matched, why in scored[:limit]]
        unmatched = [term for term, count in hit_terms.items() if count == 0]
        return {
            "matches": rows,
            "n_matches": len(scored),
            "n_considered": len(pool),
            "terms": list(cleaned),
            "unmatched_terms": unmatched,
            "suggestions": {term: self._suggest(term) for term in unmatched},
            "truncated": len(scored) > len(rows),
        }

    def _match(self, control: IndexedControl, term: str) -> tuple[int, str] | None:
        """(strength, reason) for one term against one control; ``None`` = no match.

        The order is deliberate: the plugin's own spelling is the strongest
        evidence, a role synonym is real but heuristic, and a module hit is the
        weakest (it says the control lives somewhere relevant, not that it is
        the one).

        An explicit ``#<n>`` address is **exclusive**: it names one index, and
        letting it also match a name token would turn a precise address into a
        fuzzy search (`#1` would return every control whose name contains a 1).
        """
        if term.startswith("#") and term[1:].isdigit():
            if int(term[1:]) == control.index:
                return 8, f"index {control.index}"
            return None
        if term.isdigit() and term == str(control.index):
            return 6, f"index {control.index}"
        if term in control.tokens:
            return 6, f"name token {term!r}"
        if len(term) >= 4 and any(token.startswith(term) for token in control.tokens):
            return 5, f"name token starting with {term!r}"
        if term in control.roles:
            return 4, f"role {term!r} (synonym group)"
        synonyms = [token for token in role_tokens(term) if token != term]
        for token in synonyms:
            if token in control.tokens:
                return 3, f"synonym {token!r} of {term!r}"
        if len(term) >= 4:
            slug = normalise(control.label)
            if term in slug:
                return 3, f"id contains {term!r}"
        if term in control.module_tokens:
            return 2, f"module {(control.module or '')!r} matches {term!r}"
        return None

    def _suggest(self, term: str, limit: int = 5) -> list[str]:
        """Vocabulary entries close to an unmatched term (prefix/substring only)."""
        found: list[str] = []
        for word in self._vocabulary:
            if word == term:
                continue
            if word.startswith(term) or term.startswith(word) or (len(term) >= 4 and term in word):
                found.append(word)
            if len(found) >= limit:
                break
        return found

    def find_many(self, terms: Sequence[str], **kwargs: Any) -> dict[str, Any]:
        """Alias kept explicit: one call may carry several function words."""
        return self.find(terms, **kwargs)


# ---------------------------------------------------------------------------
# One index per artifact, per process
# ---------------------------------------------------------------------------

_INDEX_CACHE: dict[tuple[str, str], ControlIndex] = {}
_CACHE_CAP = 4
_BUILD_STATS: dict[str, int] = {"indexes_built": 0, "index_hits": 0}


def load_index(
    payload: Mapping[str, Any],
    *,
    cache_key: tuple[str, str] | None = None,
) -> ControlIndex:
    """The index for this artifact - built once per (path, content digest).

    The key must be the artifact's own *content* identity. A key that omitted the
    digest would serve a stale view after a re-measurement (the artifact is
    replaced in place at the identity's path), which is the failure the prepared
    store's own digest exists to prevent.
    """
    if cache_key is None:
        return ControlIndex(payload)
    found = _INDEX_CACHE.get(cache_key)
    if found is not None:
        _BUILD_STATS["index_hits"] += 1
        return found
    index = ControlIndex(payload)
    _INDEX_CACHE[cache_key] = index
    _BUILD_STATS["indexes_built"] += 1
    while len(_INDEX_CACHE) > _CACHE_CAP:
        _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
    return index


def index_census() -> dict[str, int]:
    """How many indexes this process built and how many it served from memory."""
    return {
        **_BUILD_STATS,
        "cached": len(_INDEX_CACHE),
    }
