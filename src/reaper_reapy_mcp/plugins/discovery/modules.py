"""Module and submodule inference (P26 §10).

The structure of an unknown plugin has to be inferred from what the host says
about its parameters, so this analyzer scores several **independent** signals and
publishes both the winner and the alternatives. No single signal decides:

``section``     a host/format-declared group name, when a channel provides one
``prefix``      a repeated leading token family (``group_1_*``, ``group_2_*``)
``repetition``  ≥3 families sharing a base with an identical token multiset -
                the strongest structural signal available without a section
``tokens``      generic audio vocabulary (``cutoff``, ``attack``, ``midi``),
                used to name a module's *kind* and never to invent one
``order``       index contiguity, used to break ties between merged families
``units``/``types``  the units and classes the family's controls share

Two rules keep it honest:

- **unknown is a legal answer.** A control whose name carries no informative
  token is placed in one declared `unlabelled` module with status ``unknown``,
  rather than being distributed into a pretty tree it does not support;
- **a token is a hint, not a fact.** ``Level`` appears in several modules of the
  same plugin, so a token match alone can only ever reach ``candidate`` status;
  ``inferred`` and ``confirmed`` require a structural repetition to agree.

The vocabulary below is *domain* vocabulary (what audio controls are called), not
plugin knowledge: it contains no plugin, product, parameter or control identifier,
and every decision it feeds is reported as a `tokens` signal in the artifact.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from .types import GroupingResult, ModuleNode

__all__ = [
    "MODULE_KINDS",
    "SEMANTIC_TOKENS",
    "UNINFORMATIVE_TOKENS",
    "tokenize",
    "family_key_of",
    "infer_modules",
]

#: Module kinds the inference may publish.
MODULE_KINDS: tuple[str, ...] = (
    "oscillator",
    "filter",
    "envelope",
    "lfo",
    "modulation",
    "effects",
    "mixer",
    "voice",
    "global",
    "midi",
    "sample",
    "other",
    "unknown",
)

#: Generic audio vocabulary: token -> the kinds it suggests. A token may suggest
#: several kinds (``delay`` is an effect *and* an envelope stage), which is why a
#: match can never be more than a candidate on its own.
SEMANTIC_TOKENS: dict[str, tuple[str, ...]] = {
    "osc": ("oscillator",),
    "oscillator": ("oscillator",),
    "vco": ("oscillator",),
    "dco": ("oscillator",),
    "wavetable": ("oscillator",),
    "wave": ("oscillator",),
    "waveform": ("oscillator",),
    "shape": ("oscillator",),
    "spectral": ("oscillator",),
    "harmonics": ("oscillator",),
    "detune": ("oscillator", "voice"),
    "unison": ("voice", "oscillator"),
    "phase": ("oscillator", "lfo"),
    "sub": ("oscillator",),
    "transpose": ("oscillator", "global"),
    "octave": ("oscillator", "global"),
    "pitch": ("oscillator", "voice"),
    "tune": ("oscillator", "voice"),
    "filter": ("filter",),
    "cutoff": ("filter",),
    "frequency": ("filter", "lfo"),
    "resonance": ("filter",),
    "reso": ("filter",),
    "q": ("filter",),
    "slope": ("filter",),
    "keytrack": ("filter", "voice"),
    "ladder": ("filter",),
    "drive": ("filter", "effects"),
    "env": ("envelope",),
    "envelope": ("envelope",),
    "adsr": ("envelope",),
    "attack": ("envelope",),
    "decay": ("envelope",),
    "sustain": ("envelope",),
    "release": ("envelope",),
    "hold": ("envelope",),
    "lfo": ("lfo",),
    "random": ("lfo", "modulation"),
    "smooth": ("lfo",),
    "mod": ("modulation",),
    "modulation": ("modulation",),
    "route": ("modulation",),
    "matrix": ("modulation",),
    "amount": ("modulation",),
    "depth": ("modulation", "effects"),
    "destination": ("modulation",),
    "source": ("modulation",),
    "fx": ("effects",),
    "effect": ("effects",),
    "chorus": ("effects",),
    "reverb": ("effects",),
    "delay": ("effects", "envelope"),
    "phaser": ("effects",),
    "flanger": ("effects",),
    "distortion": ("effects",),
    "compress": ("effects",),
    "compressor": ("effects",),
    "eq": ("effects",),
    "filter_fx": ("effects",),
    "mix": ("mixer",),
    "dry": ("mixer",),
    "wet": ("mixer",),
    "volume": ("mixer",),
    "level": ("mixer",),
    "pan": ("mixer",),
    "gain": ("mixer",),
    "balance": ("mixer",),
    "send": ("mixer",),
    "output": ("mixer",),
    "bus": ("mixer",),
    "voice": ("voice",),
    "poly": ("voice",),
    "mono": ("voice",),
    "legato": ("voice",),
    "glide": ("voice",),
    "portamento": ("voice",),
    "global": ("global",),
    "master": ("global",),
    "bypass": ("global",),
    "oversample": ("global",),
    "oversampling": ("global",),
    "bpm": ("global",),
    "tempo": ("global",),
    "sync": ("global",),
    "midi": ("midi",),
    "cc": ("midi",),
    "controller": ("midi",),
    "channel": ("midi",),
    "aftertouch": ("midi",),
    "program": ("midi",),
    "sample": ("sample",),
    "sampler": ("sample",),
    "loop": ("sample",),
    "slice": ("sample",),
    "audio": ("sample",),
}

#: Tokens that carry no grouping information at all: a name made only of these
#: plus an index must not produce one module per index.
UNINFORMATIVE_TOKENS: frozenset[str] = frozenset(
    {
        "param",
        "params",
        "parameter",
        "knob",
        "control",
        "ctl",
        "knobs",
        "controlchange",
        "p",
        "v",
        "x",
        "y",
        "z",
        "a",
        "b",
        "c",
        "value",
        "val",
        "index",
        "idx",
        "id",
        "no",
        "num",
        "n",
        "generic",
    }
)

#: A family at or below this size is a *member* of a repeated structure rather
#: than a module of its own (the threshold is recorded in the artifact).
SMALL_FAMILY = 12

#: How many repeated families make the repetition signal strong enough to
#: publish a merge as ``confirmed``.
REPETITION_MIN = 3

_SPLIT_RE = re.compile(r"[^0-9a-zA-Z]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_DIGIT_RE = re.compile(r"^(.*?)(\d+)$")


def tokenize(name: str | None, *, ident: str | None = None, fallback: str | None = None) -> list[str]:
    """Split a parameter name into lowercase tokens, digits separate from stems.

    ``osc1Cutoff`` -> ``["osc", "1", "cutoff"]``; ``MIDI CC 1|1`` -> ``["midi",
    "cc", "1", "1"]``. The result is *evidence*: callers report it as a signal, so
    a name that tokenizes badly is visible rather than silently empty.
    """
    text = name if name else ident
    if not text:
        text = fallback or ""
    if not text:
        return []
    parts: list[str] = []
    for chunk in _SPLIT_RE.split(_CAMEL_RE.sub(" ", str(text))):
        if not chunk:
            continue
        match = _DIGIT_RE.match(chunk)
        if match and match.group(1):
            parts.append(match.group(1).lower())
            parts.append(match.group(2))
        elif match and not match.group(1):
            parts.append(match.group(2))
        else:
            parts.append(chunk.lower())
    return parts


def family_key_of(tokens: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    """``(family key, base tokens)`` for one control's tokens.

    The family is the base tokens up to and including the first index, so
    ``group_1_alpha`` and ``group_1_beta`` share ``group 1`` while ``group_2_beta``
    does not. A name with no index is its own family (its first token) - see
    `promote_discriminating_prefixes` for the second pass that splits such families
    when a short token separates them.
    """
    if not tokens:
        return "", ()
    base: list[str] = []
    for token in tokens:
        base.append(token)
        if token.isdigit():
            break
    else:
        return tokens[0], (tokens[0],)
    return " ".join(base), tuple(base)


def _is_discriminator(token: str) -> bool:
    """A token that separates siblings rather than naming a property.

    Only numbers and one-or-two-character tokens qualify. A three-character word is
    already a property: the P26 independent review showed that accepting every token
    of three characters or fewer splits one stereo effect (`... L Time`, `... R Time`,
    `... Mix`) into three modules, because `mix` was read as a sibling name.
    """
    return token.isdigit() or len(token) <= 2


def promote_discriminating_prefixes(
    token_lists: Sequence[Sequence[str]],
) -> list[tuple[str, tuple[str, ...]]]:
    """Family keys for a whole dataset, with the second pass applied.

    A plugin without numbering (`block_a_one`, `block_b_one`) has no index to stop
    the family at, so the first pass collapses both groups into ``block``. The
    second pass splits a family whose *second* token takes two or more values, when
    every one of those values looks like a discriminator (short or numeric) - and
    deliberately does not split when the second token is a long word
    (`section_gain`, `section_mix` are properties of one module, not two modules).

    This is a dataset-level decision on purpose: whether a token discriminates
    cannot be answered from one name.
    """
    first_pass = [family_key_of(tokens) for tokens in token_lists]
    groups: dict[str, list[Sequence[str]]] = {}
    for tokens, (_key, base) in zip(token_lists, first_pass):
        if base and not base[-1].isdigit():
            groups.setdefault(base[0], []).append(tokens)
    promoted: set[str] = set()
    for first, members in groups.items():
        seconds = {tokens[1] for tokens in members if len(tokens) > 1}
        if len(seconds) >= 2 and all(_is_discriminator(token) for token in seconds):
            promoted.add(first)
    resolved: list[tuple[str, tuple[str, ...]]] = []
    for tokens, (key, base) in zip(token_lists, first_pass):
        if base and not base[-1].isdigit() and base[0] in promoted and len(tokens) > 1:
            resolved.append((f"{key} {tokens[1]}", (base[0], tokens[1])))
        else:
            resolved.append((key, base))
    return resolved


def _tokens_without_family(tokens: Sequence[str], family_tokens: Sequence[str]) -> list[str]:
    remaining = list(tokens)
    for token in family_tokens:
        if token in remaining:
            remaining.remove(token)
    return remaining


def _kind_scores(tokens: Iterable[str]) -> dict[str, int]:
    scores: dict[str, int] = {}
    for token in tokens:
        for kind in SEMANTIC_TOKENS.get(token, ()):
            scores[kind] = scores.get(kind, 0) + 1
    return scores


def _title(key: str) -> str:
    parts: list[str] = []
    for token in key.split():
        parts.append(token.upper() if token in ("midi", "lfo", "eq", "fx", "cc", "bpm") else token.capitalize())
    return " ".join(parts)


def infer_modules(
    rows: Sequence[dict[str, Any]],
    *,
    small_family: int = SMALL_FAMILY,
    repetition_min: int = REPETITION_MIN,
) -> GroupingResult:
    """Infer the module tree from per-control rows.

    Each row needs ``index`` and either ``name`` or ``ident``; optional keys
    ``section``, ``unit`` and ``class`` refine the grouping, and any key the
    caller does not supply simply does not contribute a signal.
    """
    result = GroupingResult()
    signals_used: list[str] = []
    if any(row.get("section") for row in rows):
        signals_used.append("section")

    families: dict[str, dict[str, Any]] = {}
    uninformative: list[int] = []
    prepared: list[tuple[dict[str, Any], list[str], str, tuple[str, ...]]] = []
    for row in rows:
        index = int(row["index"])
        name = row.get("name")
        ident = row.get("ident")
        tokens = tokenize(name, ident=ident, fallback=row.get("fallback_name"))
        if not tokens or (len(tokens) == 1 and tokens[0].isdigit()):
            uninformative.append(index)
            continue
        if tokens[0] in UNINFORMATIVE_TOKENS and len(tokens) <= 2:
            uninformative.append(index)
            continue
        prepared.append((dict(row), list(tokens), tokens[0], ()))
    keys = promote_discriminating_prefixes([tokens for _row, tokens, _root, _base in prepared])
    for (row, tokens, _root, _unused), (key, base) in zip(prepared, keys):
        index = int(row["index"])
        root = base[0] if base else ""
        entry = families.setdefault(
            key,
            {
                "key": key,
                "base": root,
                "family_tokens": base,
                "controls": [],
                "remaining": [],
                "kinds": {},
                "sections": set(),
                "units": set(),
                "classes": set(),
                "indices": [],
            },
        )
        entry["controls"].append(index)
        entry["remaining"].append(_tokens_without_family(tokens, base))
        for kind, score in _kind_scores(tokens).items():
            entry["kinds"][kind] = entry["kinds"].get(kind, 0) + score
        if row.get("section"):
            entry["sections"].add(str(row["section"]))
        if row.get("unit"):
            entry["units"].add(str(row["unit"]))
        if row.get("class"):
            entry["classes"].add(str(row["class"]))
        entry["indices"].append(index)

    if families:
        signals_used.append("prefix")
    # The repetition signal: several small families sharing a base with the same
    # token shape are members of one structure, not several modules.
    merged: dict[str, list[str]] = {}
    shapes: dict[str, set[tuple[tuple[str, ...], ...]]] = {}
    for key, entry in families.items():
        merged.setdefault(entry["base"], []).append(key)
        shapes.setdefault(entry["base"], set()).add(
            tuple(sorted(tuple(sorted(remaining)) for remaining in entry["remaining"]))
        )
    nodes: list[ModuleNode] = []
    members: dict[str, list[dict[str, Any]]] = {}
    for base, keys in sorted(merged.items()):
        sizes = [len(families[key]["controls"]) for key in keys]
        same_shape = len(shapes.get(base, set())) == 1
        repeated = len(keys) >= repetition_min and same_shape and max(sizes) <= small_family
        if repeated:
            signals_used.append("repetition")
        if repeated:
            controls = [index for key in keys for index in families[key]["controls"]]
            kinds: dict[str, int] = {}
            for key in keys:
                for kind, score in families[key]["kinds"].items():
                    kinds[kind] = kinds.get(kind, 0) + score
            kind = max(kinds, key=lambda name: kinds[name]) if kinds else "unknown"
            node = ModuleNode(
                id=base,
                label=_title(base),
                kind=kind,
                status="inferred",
                confidence="medium",
                signals=["repetition", "prefix"] + (["tokens"] if kinds else []),
                controls=sorted(controls),
                alternatives=[
                    {"split": key, "controls": sorted(families[key]["controls"])} for key in sorted(keys)
                ],
                reason=(
                    f"{len(keys)} families share the base {base!r} with an identical token shape, "
                    f"each holding at most {small_family} controls: one module with member submodules "
                    "(the per-family split is recorded as an alternative, because a repeated shape is "
                    "evidence of a shared structure and not of a single module)"
                ),
            )
            nodes.append(node)
            members[node.id] = [families[key] for key in keys]
            continue
        for key in keys:
            entry = families[key]
            scores = entry["kinds"]
            kind = max(scores, key=lambda name: scores[name]) if scores else "unknown"
            ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
            section = sorted(entry["sections"])
            signals = ["prefix"]
            status = "candidate"
            confidence = "weak"
            if section:
                signals.insert(0, "section")
                status = "confirmed"
                confidence = "strong"
            elif scores and len(entry["controls"]) >= 2:
                signals.append("tokens")
                status = "inferred"
                confidence = "weak" if len(scores) > 1 else "medium"
            elif scores:
                signals.append("tokens")
            if len(entry["units"]) == 1 and entry["units"]:
                signals.append("units")
            if len(entry["classes"]) == 1 and entry["classes"]:
                signals.append("types")
            if len(entry["indices"]) >= 2 and max(entry["indices"]) - min(entry["indices"]) == len(entry["indices"]) - 1:
                signals.append("order")
            node = ModuleNode(
                id=key,
                label=(section[0] if section else _title(key)),
                kind=kind,
                status=status,
                confidence=confidence,
                signals=signals,
                controls=sorted(entry["controls"]),
                alternatives=[
                    {"kind": other, "score": score} for other, score in ranked[1:3]
                ],
                reason=None
                if kind != "unknown"
                else "no vocabulary token matched this family's name",
            )
            nodes.append(node)
            members[node.id] = [entry]

    children: list[ModuleNode] = []
    for node in nodes:
        children.extend(_submodules_of(node, members.get(node.id, [])))
    nodes.extend(children)
    if children:
        signals_used.append("repetition")

    if uninformative:
        nodes.append(
            ModuleNode(
                id="unlabelled",
                label="Unlabelled controls",
                kind="unknown",
                status="unknown",
                confidence="none",
                signals=[],
                controls=sorted(uninformative),
                reason=(
                    "the host exposes no informative name for these parameters; they are kept "
                    "together rather than distributed into inferred modules"
                ),
            )
        )
    nodes.sort(key=lambda node: (1 if node.parent else 0, min(node.controls) if node.controls else 1 << 30, node.id))
    result.nodes = nodes
    result.signals_used = sorted(set(signals_used))
    placed = len({index for node in nodes if not node.parent for index in node.controls})
    if placed != len(rows):
        result.notes.append(f"{len(rows) - placed} row(s) were not placed by the inference")
    return result


def _submodules_of(node: ModuleNode, entries: Sequence[dict[str, Any]]) -> list[ModuleNode]:
    """Child nodes for a family whose members split by a repeated second token.

    Only a *repeated* second token becomes a submodule (≥2 controls); a one-off
    token must not invent a level of the tree, and a family with fewer than six
    controls is too small for a level of its own.
    """
    if len(node.controls) < 6 or not entries:
        return []
    buckets: dict[str, list[int]] = {}
    for entry in entries:
        for index, remaining in zip(entry["controls"], entry["remaining"]):
            for token in remaining:
                if token.isdigit() or token in UNINFORMATIVE_TOKENS:
                    continue
                buckets.setdefault(token, []).append(index)
                break
    groups = {token: sorted(set(indices)) for token, indices in buckets.items() if len(set(indices)) >= 2}
    if len(groups) < 2:
        return []
    children: list[ModuleNode] = []
    for ordinal, (token, indices) in enumerate(sorted(groups.items())):
        kind_scores = _kind_scores(tokenize(token))
        kind = max(kind_scores, key=lambda name: kind_scores[name]) if kind_scores else node.kind
        children.append(
            ModuleNode(
                id=f"{node.id} / {token}",
                label=_title(token),
                parent=node.id,
                kind=kind,
                status="inferred",
                confidence="weak",
                signals=["repetition", "prefix", "tokens" if kind_scores else "prefix"],
                controls=indices,
                reason=f"{len(indices)} controls of {node.id!r} repeat the token {token!r}",
            )
        )
    return children
