"""Facts read off the plugin's own parameter table, verified against this build.

**Where every number below comes from.** Vital is free software, and its parameter
table is the authority for what each parameter *is*: ``src/common/synth_parameters.cpp``
declares every ``ValueDetails`` as::

    {name, version_added, min, max, default_value, post_offset, display_multiply,
     value_scale, display_invert, display_units, display_name, string_lookup}

and expands the per-instance groups (``env_1..6``, ``lfo_1..8``, ``random_1..4``,
``osc_1..3``, ``filter_1..2``, ``filter_fx``, ``modulation_1..64``). The text the
plugin hands the host is produced by ``src/plugin/value_bridge.h``::

    adjusted = plugin_value * (max - min) + min        # kIndexed: std::round
    text     = (display_multiply * skew(adjusted) + post_offset) + units

with ``skew`` = linear / x**2 / x**3 / x**4 / sqrt(x) / 2**x (``2**-x`` when
``display_invert``), printed as a float with six significant digits and trimmed.
A ``string_lookup`` replaces the number with a label from ``synth_strings.h``.

**The source was checked against the loaded plugin, not assumed.** The public branch
is not the released binary (it has no ``osc_N_spectral_morph_phase``, and prints
``" secs"`` where the release prints ``" s"``), so the model above was run against
the **live host formatter** samples in the P22 calibration artifact - every numeric
control it covers, 17 grid points each (measured 2026-10-12, Vital VST3 in REAPER
7.53)::

    7,650 samples compared   7,565 texts identical
                              85 equal numbers whose unit label differs
                               0 numbers differ
    absent from the source: osc_1..3_spectral_morph_phase

The 85 label differences are five controls (``chorus_frequency``,
``delay_aux_frequency``, ``flanger_frequency``, ``phaser_frequency``: ``secs`` vs
``s``; ``voice_tune``: ``cents`` vs ``c``) - the release's own strings differ from
the public branch there, which changes no number.

**What went wrong without it.** ``_range_from_row`` took ``native_min``/``native_max``
from the registry, which are the extremes seen in a 900-preset corpus
(``phase2b_native_ranges.json``) - a *sample*, not the parameter's bounds. Of the 452
numeric controls in the served profile, 225 take their range from that declaration
(the others are fitted to the measured sweep, or were measured live, and live in the
display's own space)::

    155 already equal to the source's bounds
     70 strictly narrower - the corpus truncated the parameter
      0 shifted, 0 wider

The 70 are ``SOURCE_BOUNDS`` below. ``widen_to_source_bounds`` is the only consumer,
it widens only, and it never touches a row whose range was *measured* rather than
declared.

**What is deliberately not claimed.** The bounds are the plugin's own parameter
bounds, so a value inside them is *representable*; the display text at both extremes
is corroborated by the live formatter (``DISPLAY_FACTS.*.live_text_at_0/1``), which
is the strongest evidence this channel can give without writing. Neither is a claim
that a *write* of every value in the range was exercised - nothing here was written.
"""


#: The correspondence measurement above, in one dict, so a test can pin it.
CORRESPONDENCE = {
    "source": "mtytel/vital main: src/common/synth_parameters.cpp, src/plugin/value_bridge.h, "
    "src/interface/look_and_feel/synth_strings.h, src/common/synth_constants.h, src/common/common.h",
    "channel": "live host formatter samples (P22 vital_host_calibration.json: 17 grid points per control)",
    "sampled_controls": 450,
    "samples_compared": 7650,
    "texts_identical": 7565,
    "numbers_equal_unit_label_differs": 85,
    "numbers_differ": 0,
    "absent_from_the_source": ["osc_1_spectral_morph_phase", "osc_2_spectral_morph_phase", "osc_3_spectral_morph_phase"],
    "unit_label_drift": {
        "chorus_frequency": "'secs' vs 's'",
        "delay_aux_frequency": "'secs' vs 's'",
        "flanger_frequency": "'secs' vs 's'",
        "phaser_frequency": "'secs' vs 's'",
        "voice_tune": "'cents' vs 'c'",
    },
    "state_space_rows": {"equal": 155, "truncated": 70, "shifted": 0, "wider": 0},
    "display_space_rows_not_comparable": 227,
}

#: control id -> the plugin's own state-space bounds (``min``/``max`` in the
#: ``ValueDetails`` table). Only the controls whose declared range was a strict
#: subset of these are listed; the other 155 state-space rows already equal them.
SOURCE_BOUNDS: dict[str, dict[str, float]] = {
    "delay_feedback": {"min": -1.0, "max": 1.0},
    "eq_band_resonance": {"min": 0.0, "max": 1.0},
    "eq_high_cutoff": {"min": 8.0, "max": 136.0},
    "eq_high_gain": {"min": -15.0, "max": 15.0},
    "eq_high_resonance": {"min": 0.0, "max": 1.0},
    "eq_low_cutoff": {"min": 8.0, "max": 136.0},
    "eq_low_resonance": {"min": 0.0, "max": 1.0},
    "filter_1_formant_spread": {"min": -1.0, "max": 1.0},
    "filter_fx_formant_spread": {"min": -1.0, "max": 1.0},
    "filter_fx_formant_transpose": {"min": -12.0, "max": 12.0},
    "filter_fx_formant_x": {"min": 0.0, "max": 1.0},
    "flanger_center": {"min": 8.0, "max": 136.0},
    "flanger_feedback": {"min": -1.0, "max": 1.0},
    "lfo_1_frequency": {"min": -7.0, "max": 9.0},
    "lfo_1_stereo": {"min": -0.5, "max": 0.5},
    "lfo_2_frequency": {"min": -7.0, "max": 9.0},
    "lfo_3_frequency": {"min": -7.0, "max": 9.0},
    "lfo_3_stereo": {"min": -0.5, "max": 0.5},
    "lfo_4_frequency": {"min": -7.0, "max": 9.0},
    "lfo_4_smooth_time": {"min": -10.0, "max": 4.0},
    "lfo_5_frequency": {"min": -7.0, "max": 9.0},
    "lfo_6_frequency": {"min": -7.0, "max": 9.0},
    "lfo_6_smooth_time": {"min": -10.0, "max": 4.0},
    "lfo_6_stereo": {"min": -0.5, "max": 0.5},
    "lfo_7_frequency": {"min": -7.0, "max": 9.0},
    "lfo_7_stereo": {"min": -0.5, "max": 0.5},
    "lfo_8_frequency": {"min": -7.0, "max": 9.0},
    "lfo_8_stereo": {"min": -0.5, "max": 0.5},
    "modulation_10_power": {"min": -10.0, "max": 10.0},
    "modulation_11_power": {"min": -10.0, "max": 10.0},
    "modulation_12_power": {"min": -10.0, "max": 10.0},
    "modulation_13_power": {"min": -10.0, "max": 10.0},
    "modulation_15_power": {"min": -10.0, "max": 10.0},
    "modulation_16_power": {"min": -10.0, "max": 10.0},
    "modulation_18_power": {"min": -10.0, "max": 10.0},
    "modulation_1_power": {"min": -10.0, "max": 10.0},
    "modulation_20_power": {"min": -10.0, "max": 10.0},
    "modulation_2_power": {"min": -10.0, "max": 10.0},
    "modulation_30_power": {"min": -10.0, "max": 10.0},
    "modulation_33_power": {"min": -10.0, "max": 10.0},
    "modulation_34_power": {"min": -10.0, "max": 10.0},
    "modulation_35_power": {"min": -10.0, "max": 10.0},
    "modulation_3_power": {"min": -10.0, "max": 10.0},
    "modulation_47_amount": {"min": -1.0, "max": 1.0},
    "modulation_49_amount": {"min": -1.0, "max": 1.0},
    "modulation_4_power": {"min": -10.0, "max": 10.0},
    "modulation_50_amount": {"min": -1.0, "max": 1.0},
    "modulation_52_amount": {"min": -1.0, "max": 1.0},
    "modulation_54_amount": {"min": -1.0, "max": 1.0},
    "modulation_55_amount": {"min": -1.0, "max": 1.0},
    "modulation_56_amount": {"min": -1.0, "max": 1.0},
    "modulation_57_amount": {"min": -1.0, "max": 1.0},
    "modulation_58_amount": {"min": -1.0, "max": 1.0},
    "modulation_59_amount": {"min": -1.0, "max": 1.0},
    "modulation_5_power": {"min": -10.0, "max": 10.0},
    "modulation_60_amount": {"min": -1.0, "max": 1.0},
    "modulation_61_amount": {"min": -1.0, "max": 1.0},
    "modulation_62_amount": {"min": -1.0, "max": 1.0},
    "modulation_63_amount": {"min": -1.0, "max": 1.0},
    "modulation_64_amount": {"min": -1.0, "max": 1.0},
    "modulation_6_power": {"min": -10.0, "max": 10.0},
    "modulation_7_power": {"min": -10.0, "max": 10.0},
    "modulation_8_power": {"min": -10.0, "max": 10.0},
    "modulation_9_power": {"min": -10.0, "max": 10.0},
    "phaser_center": {"min": 8.0, "max": 136.0},
    "pitch_wheel": {"min": -1.0, "max": 1.0},
    "sample_pan": {"min": -1.0, "max": 1.0},
    "sample_tune": {"min": -1.0, "max": 1.0},
    "velocity_track": {"min": -1.0, "max": 1.0},
    "voice_tune": {"min": -1.0, "max": 1.0},
}

#: control id -> how the plugin renders its value as text, plus the text the *live
#: host* printed at normalized 0 and 1 (the same two points ``source_bounds``
#: widens to). ``text_at_*`` is recomputed from the table above; the two agreeing is
#: the check a reader can repeat offline.
DISPLAY_FACTS: dict[str, dict[str, object]] = {
    "delay_feedback": {"multiply": 100.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '%', "text_at_0": '-100%', "text_at_1": '100%', "live_text_at_0": '-100%', "live_text_at_1": '100%'},
    "eq_band_resonance": {"multiply": 100.0, "scale": "quadratic", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '0', "text_at_1": '100', "live_text_at_0": '0', "live_text_at_1": '100'},
    "eq_high_cutoff": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": ' semitones', "text_at_0": '8 semitones', "text_at_1": '136 semitones', "live_text_at_0": '8 semitones', "live_text_at_1": '136 semitones'},
    "eq_high_gain": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": ' dB', "text_at_0": '-15 dB', "text_at_1": '15 dB', "live_text_at_0": '-15 dB', "live_text_at_1": '15 dB'},
    "eq_high_resonance": {"multiply": 100.0, "scale": "quadratic", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '0', "text_at_1": '100', "live_text_at_0": '0', "live_text_at_1": '100'},
    "eq_low_cutoff": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": ' semitones', "text_at_0": '8 semitones', "text_at_1": '136 semitones', "live_text_at_0": '8 semitones', "live_text_at_1": '136 semitones'},
    "eq_low_resonance": {"multiply": 100.0, "scale": "quadratic", "invert": False, "post_offset": 0.0, "units": '%', "text_at_0": '0%', "text_at_1": '100%', "live_text_at_0": '0%', "live_text_at_1": '100%'},
    "filter_1_formant_spread": {"multiply": 100.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '%', "text_at_0": '-100%', "text_at_1": '100%', "live_text_at_0": '-100%', "live_text_at_1": '100%'},
    "filter_fx_formant_spread": {"multiply": 100.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '%', "text_at_0": '-100%', "text_at_1": '100%', "live_text_at_0": '-100%', "live_text_at_1": '100%'},
    "filter_fx_formant_transpose": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-12', "text_at_1": '12', "live_text_at_0": '-12', "live_text_at_1": '12'},
    "filter_fx_formant_x": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '0', "text_at_1": '1', "live_text_at_0": '0', "live_text_at_1": '1'},
    "flanger_center": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": ' semitones', "text_at_0": '8 semitones', "text_at_1": '136 semitones', "live_text_at_0": '8 semitones', "live_text_at_1": '136 semitones'},
    "flanger_feedback": {"multiply": 100.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '%', "text_at_0": '-100%', "text_at_1": '100%', "live_text_at_0": '-100%', "live_text_at_1": '100%'},
    "lfo_1_frequency": {"multiply": 1.0, "scale": "exponential", "invert": True, "post_offset": 0.0, "units": ' secs', "text_at_0": '128 secs', "text_at_1": '0.00195312 secs', "live_text_at_0": '128 secs', "live_text_at_1": '0.00195312 secs'},
    "lfo_1_stereo": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-0.5', "text_at_1": '0.5', "live_text_at_0": '-0.5', "live_text_at_1": '0.5'},
    "lfo_2_frequency": {"multiply": 1.0, "scale": "exponential", "invert": True, "post_offset": 0.0, "units": ' secs', "text_at_0": '128 secs', "text_at_1": '0.00195312 secs', "live_text_at_0": '128 secs', "live_text_at_1": '0.00195312 secs'},
    "lfo_3_frequency": {"multiply": 1.0, "scale": "exponential", "invert": True, "post_offset": 0.0, "units": ' secs', "text_at_0": '128 secs', "text_at_1": '0.00195312 secs', "live_text_at_0": '128 secs', "live_text_at_1": '0.00195312 secs'},
    "lfo_3_stereo": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-0.5', "text_at_1": '0.5', "live_text_at_0": '-0.5', "live_text_at_1": '0.5'},
    "lfo_4_frequency": {"multiply": 1.0, "scale": "exponential", "invert": True, "post_offset": 0.0, "units": ' secs', "text_at_0": '128 secs', "text_at_1": '0.00195312 secs', "live_text_at_0": '128 secs', "live_text_at_1": '0.00195312 secs'},
    "lfo_4_smooth_time": {"multiply": 1.0, "scale": "exponential", "invert": False, "post_offset": 0.0, "units": ' secs', "text_at_0": '0.000976562 secs', "text_at_1": '16 secs', "live_text_at_0": '0.000976562 secs', "live_text_at_1": '16 secs'},
    "lfo_5_frequency": {"multiply": 1.0, "scale": "exponential", "invert": True, "post_offset": 0.0, "units": ' secs', "text_at_0": '128 secs', "text_at_1": '0.00195312 secs', "live_text_at_0": '128 secs', "live_text_at_1": '0.00195312 secs'},
    "lfo_6_frequency": {"multiply": 1.0, "scale": "exponential", "invert": True, "post_offset": 0.0, "units": ' secs', "text_at_0": '128 secs', "text_at_1": '0.00195312 secs', "live_text_at_0": '128 secs', "live_text_at_1": '0.00195312 secs'},
    "lfo_6_smooth_time": {"multiply": 1.0, "scale": "exponential", "invert": False, "post_offset": 0.0, "units": ' secs', "text_at_0": '0.000976562 secs', "text_at_1": '16 secs', "live_text_at_0": '0.000976562 secs', "live_text_at_1": '16 secs'},
    "lfo_6_stereo": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-0.5', "text_at_1": '0.5', "live_text_at_0": '-0.5', "live_text_at_1": '0.5'},
    "lfo_7_frequency": {"multiply": 1.0, "scale": "exponential", "invert": True, "post_offset": 0.0, "units": ' secs', "text_at_0": '128 secs', "text_at_1": '0.00195312 secs', "live_text_at_0": '128 secs', "live_text_at_1": '0.00195312 secs'},
    "lfo_7_stereo": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-0.5', "text_at_1": '0.5', "live_text_at_0": '-0.5', "live_text_at_1": '0.5'},
    "lfo_8_frequency": {"multiply": 1.0, "scale": "exponential", "invert": True, "post_offset": 0.0, "units": ' secs', "text_at_0": '128 secs', "text_at_1": '0.00195312 secs', "live_text_at_0": '128 secs', "live_text_at_1": '0.00195312 secs'},
    "lfo_8_stereo": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-0.5', "text_at_1": '0.5', "live_text_at_0": '-0.5', "live_text_at_1": '0.5'},
    "modulation_10_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_11_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_12_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_13_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_15_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_16_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_18_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_1_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_20_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_2_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_30_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_33_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_34_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_35_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_3_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_47_amount": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-1', "text_at_1": '1', "live_text_at_0": '-1', "live_text_at_1": '1'},
    "modulation_49_amount": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-1', "text_at_1": '1', "live_text_at_0": '-1', "live_text_at_1": '1'},
    "modulation_4_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_50_amount": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-1', "text_at_1": '1', "live_text_at_0": '-1', "live_text_at_1": '1'},
    "modulation_52_amount": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-1', "text_at_1": '1', "live_text_at_0": '-1', "live_text_at_1": '1'},
    "modulation_54_amount": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-1', "text_at_1": '1', "live_text_at_0": '-1', "live_text_at_1": '1'},
    "modulation_55_amount": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-1', "text_at_1": '1', "live_text_at_0": '-1', "live_text_at_1": '1'},
    "modulation_56_amount": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-1', "text_at_1": '1', "live_text_at_0": '-1', "live_text_at_1": '1'},
    "modulation_57_amount": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-1', "text_at_1": '1', "live_text_at_0": '-1', "live_text_at_1": '1'},
    "modulation_58_amount": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-1', "text_at_1": '1', "live_text_at_0": '-1', "live_text_at_1": '1'},
    "modulation_59_amount": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-1', "text_at_1": '1', "live_text_at_0": '-1', "live_text_at_1": '1'},
    "modulation_5_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_60_amount": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-1', "text_at_1": '1', "live_text_at_0": '-1', "live_text_at_1": '1'},
    "modulation_61_amount": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-1', "text_at_1": '1', "live_text_at_0": '-1', "live_text_at_1": '1'},
    "modulation_62_amount": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-1', "text_at_1": '1', "live_text_at_0": '-1', "live_text_at_1": '1'},
    "modulation_63_amount": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-1', "text_at_1": '1', "live_text_at_0": '-1', "live_text_at_1": '1'},
    "modulation_64_amount": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-1', "text_at_1": '1', "live_text_at_0": '-1', "live_text_at_1": '1'},
    "modulation_6_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_7_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_8_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "modulation_9_power": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-10', "text_at_1": '10', "live_text_at_0": '-10', "live_text_at_1": '10'},
    "phaser_center": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": ' semitones', "text_at_0": '8 semitones', "text_at_1": '136 semitones', "live_text_at_0": '8 semitones', "live_text_at_1": '136 semitones'},
    "pitch_wheel": {"multiply": 1.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-1', "text_at_1": '1', "live_text_at_0": '-1', "live_text_at_1": '1'},
    "sample_pan": {"multiply": 100.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '%', "text_at_0": '-100%', "text_at_1": '100%', "live_text_at_0": '-100%', "live_text_at_1": '100%'},
    "sample_tune": {"multiply": 100.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '', "text_at_0": '-100', "text_at_1": '100', "live_text_at_0": '-100', "live_text_at_1": '100'},
    "velocity_track": {"multiply": 100.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": '%', "text_at_0": '-100%', "text_at_1": '100%', "live_text_at_0": '-100%', "live_text_at_1": '100%'},
    "voice_tune": {"multiply": 100.0, "scale": "linear", "invert": False, "post_offset": 0.0, "units": ' cents', "text_at_0": '-100 cents', "text_at_1": '100 cents', "live_text_at_0": '-100 c', "live_text_at_1": '100 c'},
}

#: The two controls whose engine value is an **integer** by construction. Both are
#: ``ValueDetails::kIndexed`` with a span of 10,000s, so ``ValueBridge::isDiscrete()``
#: is false (it requires span < 300) and the plugin tells the host the parameter is
#: continuous - REAPER therefore reports no step (measured: ``TrackFX_GetParameterStepSizes``
#: reported=false, step=0, for both). But ``convertToEngineValue`` rounds *unconditionally*
#: for kIndexed, so every value the **host write channel** stores is an integer (the
#: display, which rounds before printing, is an integer too, which is why 70 arbitrary
#: points never showed a fraction). An engine-set value bypasses that conversion - out
#: of the profile's write path, and no fractional-reachable state was found. What is
#: claimed here is the write channel: a fractional request is stored rounded, which is
#: storage quantisation, not a formatting accident.
INTEGER_LADDERS: dict[str, dict[str, object]] = {
    "effect_chain_order": {
        "min": 0.0,
        "max": 362879.0,
        "step": 1.0,
        "span_explanation": "factorial(kNumEffects) - 1 = 9! - 1 = 362879: the value is a permutation index of the 9 effects",
        "vst3_continuity": "isDiscrete() is false (span >= 300): the host sees a continuous parameter and reports no step",
        "plugin_rounding": "ValueBridge::convertToEngineValue rounds every kIndexed set (std::round, halfway away from zero)",
    },
    "sample_transpose_quantize": {
        "min": 0.0,
        "max": 8191.0,
        "step": 1.0,
        "span_explanation": "0..8191 = 2**13 - 1 whole semitone-quantisation codes",
        "vst3_continuity": "isDiscrete() is false (span >= 300): the host sees a continuous parameter and reports no step",
        "plugin_rounding": "ValueBridge::convertToEngineValue rounds every kIndexed set (std::round, halfway away from zero)",
    },
}

#: The controls whose *plugin-side* display has no label table at all, so the host
#: prints a number. ``member_ids`` records what the profile calls the states and where
#: those words came from; **no authoritative display label exists** for either state.
NO_LABEL_TABLE: dict[str, dict[str, object]] = {
    "bypass": {
        "source_shape": '{"bypass", 0x000702, 0.0, 1.0, 0.0, 0.0, 60.0, ValueDetails::kIndexed, false, "", "Bypass", nullptr}',
        "display_multiply": 60.0,
        "string_lookup": None,
        "consequence": "the plugin's own text is the value times 60 with no label: the host prints 0 at one state and 60 at the other",
        "live_text": ["0", "60"],
        "reaper_step_channel": "TrackFX_GetParameterStepSizes: is_toggle=true, step=1.0 - the host itself calls it a two-state toggle",
        "member_ids": ["normal", "bypassed"],
        "member_ids_source": (
            "the registry's own sweep channel (vital_scales_full.json recorded the display "
            "strings 'normal'/'bypassed'), not the plugin's display function"
        ),
    },
}

#: ``filter_N_style``: five labels for a parameter that can hold more states than
#: that.
#:
#: **The bounds are the release's, not the public branch's.** The public table
#: declares ``0.0 .. 9.0``, which would put the first label change at normalized
#: ``0.5/9 = 0.0556``; the P23 ULP bisection measured it at ``0.0625 - 2**-29``, and
#: the next three at 0.1875, 0.3125 and 0.4375 - i.e. ``(k + 0.5)/8``, a span of
#: **8**. ``ValueBridge::span_`` is ``round(max - min)`` for kIndexed, so the release
#: must declare ``max = 8.0`` there. The measurement wins (it is the host's own
#: formatter, read at arbitrary doubles), and it is also what the registry's own
#: 1/16 sweep recorded.
FILTER_STYLE_FACTS: dict[str, object] = {
    "parameter": "filter_{1,2,fx}_style",
    "labels": ["12dB", "24dB", "Notch Blend", "Notch Spread", "B/P/N"],
    "labels_source": "src/interface/look_and_feel/synth_strings.h (kFilterStyleNames), used by value_bridge.h getText",
    "parameter_bounds": {"min": 0.0, "max": 8.0},
    "parameter_bounds_source": (
        "the release as measured: P23's ULP bisection places the label changes at "
        "0.0625, 0.1875, 0.3125, 0.4375 = (k + 0.5)/8, while the public branch's "
        "0.0..9.0 would place the first at 0.0556"
    ),
    "public_source_bounds": {"min": 0.0, "max": 9.0},
    "value_scale": "indexed",
    "named_indices": [0, 1, 2, 3, 4],
    "unnamed_indices": [5, 6, 7, 8],
    "unnamed_reason": (
        "getText indexes the five-entry label array with the rounded value, clamped only at the "
        "top, so any index past 4 reads past the table: there is no name to print, and the "
        "release prints an empty string (the registry sweep recorded '' at 0.5625 = 4.5/8, "
        "which is the transition into that region)"
    ),
    "display_depends_on_the_filter_model": False,
    "display_depends_on_the_filter_model_why": (
        "value_bridge.h getText uses kFilterStyleNames unconditionally; the model-dependent naming "
        "lives in the editor (src/interface/editor_sections/filter_section.cpp getStyleName(model, style)), "
        "which is a different channel and does not reach the parameter's text"
    ),
    "ui_labels_by_model": {
        "Analog/Dirty/Ladder/Digital": ["12dB", "24dB", "Notch Blend", "Notch Spread", "B/P/N"],
        "Diode": ["Low Shelf", "Low Cut"],
        "Formant": ["The Mouth", "AIUO", "AOIE"],
        "Comb": ["Low High Comb", "Low High Flange+", "Low High Flange-", "Band Spread Comb", "Band Spread Flange+", "Band Spread Flange-"],
        "Phase": ["Positive", "Negative"],
    },
    "ui_labels_source": "src/interface/editor_sections/filter_section.cpp (getNumStyles/getStyleName)",
}

#: The live controls the public source does not declare, so nothing in this module
#: says anything about them.
NOT_IN_SOURCE = {
    "osc_1_spectral_morph_phase": "the public branch has no such parameter (the release does); no source fact is claimed",
    "osc_2_spectral_morph_phase": "the public branch has no such parameter (the release does); no source fact is claimed",
    "osc_3_spectral_morph_phase": "the public branch has no such parameter (the release does); no source fact is claimed",
}


def source_bounds_for(control_id: str) -> dict[str, float] | None:
    """The plugin's own bounds for ``control_id``, when they are known wider ones."""
    bounds = SOURCE_BOUNDS.get(str(control_id))
    return dict(bounds) if bounds else None


def widen_to_source_bounds(control_id: str, declared: dict | None) -> dict | None:
    """The correction record when ``declared`` is a strict subset of the source's bounds.

    Never narrows and never shifts: a declared range that is not a subset returns
    ``None`` untouched (measured 2026-10-12: of the 225 state-space rows, 155 are
    equal, 70 are strict subsets, 0 are wider or shifted). The caller decides what to
    do with the record; the profile keeps the *before* values inside it.

    ``declared`` is the range the profile would otherwise serve - ``{"min", "max"}``
    from the registry's corpus-derived bounds, or ``None`` when the row has none (an
    absent range is not a truncation and is left as it is).
    """
    bounds = SOURCE_BOUNDS.get(str(control_id))
    if bounds is None or not isinstance(declared, dict):
        return None
    low = declared.get("min")
    high = declared.get("max")
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    low, high = float(low), float(high)
    source_low, source_high = float(bounds["min"]), float(bounds["max"])
    tolerance = 1e-6 * max(1.0, abs(source_low), abs(source_high))
    if low >= source_low - tolerance and high <= source_high + tolerance:
        if abs(low - source_low) <= tolerance and abs(high - source_high) <= tolerance:
            return None
        return {
            "declared_before": {"min": low, "max": high},
            "min": source_low,
            "max": source_high,
            "widened": {
                "min": source_low if low > source_low else None,
                "max": source_high if high < source_high else None,
            },
        }
    return None
