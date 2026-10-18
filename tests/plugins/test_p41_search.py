"""P41: the preset search RANKS, says why a row matched, and carries only what a caller can act on.

MEASURED (2026-10-18, live, through `plugin_presets(op="search")`, one real call each):

* a one-token query "ld" answered **140 rows** - every name that merely CONTAINS the letters
  (battlefield, cold, ...) plus the true LD presets, alphabetically interleaved. Nothing in the
  answer said which row was the one the caller meant, so the caller had to page and judge;
* the answer for `limit=40` was **~18 KB**, of which ~11 KB was rows. Each row carried
  `mtime_ns` (19 digits of nanoseconds - no caller can act on it) and an `embedded_name` that
  repeats the file-name-derived `name` in most of the corpus;
* a token that matched nothing still narrowed nothing visibly, and there was no author filter
  although the library is organised by pack.

The search now scores every token by the BEST field it hits (whole word > word prefix > substring,
weighted name > embedded name > author > id), sorts by score, names the fields that scored
(`matched_in`), reports tokens that hit nothing (`unmatched_terms`), filters by `author`, and drops
the two per-row fields no caller can use.

Hermetic: every library is built in `tmp_path` from synthetic documents.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[2] / "src" / "reaper_reapy_mcp" / "plugins"
if str(PLUGINS.parents[1]) not in sys.path:
    sys.path.insert(0, str(PLUGINS.parents[1]))

from reaper_reapy_mcp.plugins.errors import InvalidValue  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import catalog as CAT  # noqa: E402

import test_p38_presets as P38  # noqa: E402


def library(tmp_path: Path) -> CAT.Catalog:
    """The shape the live queries met: true LD presets among names that merely contain "ld"."""
    root = tmp_path / "lib"
    rows = (
        ("User/Presets/Pack A/LD - Future Saw.vital", "LD - Future Saw", "Florixel", "Lead"),
        ("User/Presets/Pack A/LD Detonator.vital", "LD Detonator", "Frantic Noise", "Lead"),
        ("User/Presets/Pack B/Battlefield.vital", "Battlefield", "SNFK", "Bass"),
        ("User/Presets/Pack B/Cold Planet.vital", "Cold Planet", "SNFK", "Keys"),
        ("User/Presets/Pack C/Deep Bass.vital", "Deep Bass", "Ann", "Bass"),
    )
    for relative, name, author, style in rows:
        P38.write_preset(
            root, relative, P38.preset_document(name=name, author=author, style=style)
        )
    return CAT.Catalog(root, directory=tmp_path / "cache")


def test_a_whole_word_hit_outranks_a_substring_hit(tmp_path: Path):
    """The live complaint in one assertion: "ld" must answer the LD presets FIRST."""
    catalog = library(tmp_path)
    result = catalog.search(query="ld")
    names = [row["name"] for row in result["rows"]]
    assert names[:2] == ["LD - Future Saw", "LD Detonator"], names
    substring_only = [row for row in result["rows"] if "ld" not in row["name"].lower().split()]
    assert substring_only, "a substring match still answers - it is ranked, not hidden"
    assert all(row["score"] < result["rows"][0]["score"] for row in substring_only)
    # every row says WHY it matched, so a poor match is visibly poor rather than merely early
    assert result["rows"][0]["matched_in"] == ["name"]
    assert "id" in substring_only[0]["matched_in"] or substring_only[0]["matched_in"] == ["name"]


def test_a_word_prefix_hit_sits_between_a_word_and_a_substring(tmp_path: Path):
    """A caller typing half a word gets the word's rows before the coincidences."""
    catalog = library(tmp_path)
    result = catalog.search(query="deto")
    assert result["rows"][0]["name"] == "LD Detonator"
    assert result["rows"][0]["score"] > 12, result["rows"][0]["score"]
    # and a prefix hit is worth less than the whole word it is a prefix of
    whole_word = catalog.search(query="detonator")["rows"][0]["score"]
    assert whole_word > result["rows"][0]["score"]


def test_a_token_that_hits_nothing_is_named_and_the_others_still_answer(tmp_path: Path):
    catalog = library(tmp_path)
    result = catalog.search(query="detonator zzz")
    assert result["terms"] == ["detonator", "zzz"]
    assert result["unmatched_terms"] == ["zzz"]
    assert [row["name"] for row in result["rows"]] == ["LD Detonator"]


def test_the_author_filter_narrows_to_a_pack(tmp_path: Path):
    catalog = library(tmp_path)
    result = catalog.search(author="snfk")
    assert {row["name"] for row in result["rows"]} == {"Battlefield", "Cold Planet"}
    assert result["total"] == 2
    # the filter combines with a query instead of replacing it
    combined = catalog.search(query="cold", author="snfk")
    assert [row["name"] for row in combined["rows"]] == ["Cold Planet"]


def test_a_row_carries_only_what_a_caller_can_act_on(tmp_path: Path):
    """`mtime_ns` is not an argument to anything, and a repeated embedded name is noise."""
    catalog = library(tmp_path)
    row = catalog.search(query="detonator")["rows"][0]
    assert "mtime_ns" not in row
    assert "embedded_name" not in row, "it equals `name` here, so it is omitted"
    assert set(row) == {"id", "name", "author", "style", "synth_version", "size", "score", "matched_in"}


def test_a_row_keeps_an_embedded_name_that_differs(tmp_path: Path):
    """The embedded name is real data when it differs (549 corpus files carry none, some differ)."""
    root = tmp_path / "lib2"
    document = P38.preset_document(name="File Name", author="A", style="Lead")
    P38.write_preset(root, "User/Presets/file_name.vital", document)
    # the writer serializes the document's own `preset_name`; patch it the way a pack does
    path = root / "User" / "Presets" / "file_name.vital"
    raw = path.read_bytes().replace(b'"preset_name":"File Name"', b'"preset_name":"Embedded One"')
    path.write_bytes(raw)
    row = CAT.Catalog(root, directory=tmp_path / "cache2").search(query="embedded")["rows"][0]
    assert row["embedded_name"] == "Embedded One"


def test_the_limit_and_offset_are_still_refused_when_invalid(tmp_path: Path):
    catalog = library(tmp_path)
    with pytest.raises(InvalidValue):
        catalog.search(limit=0)
    with pytest.raises(InvalidValue):
        catalog.search(offset=-1)
    with pytest.raises(InvalidValue):
        catalog.search(limit="many")


def test_the_response_reports_the_ranking_it_applied(tmp_path: Path):
    result = library(tmp_path).search(query="ld")
    assert "whole word" in result["ranking"]
    assert result["library"]["files"] == 5
    assert json.dumps(result)  # serializable end to end: it is a wire answer, not a model
