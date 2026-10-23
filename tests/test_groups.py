import re

import pytest

from billing_usage import PRICES
from usage_groups import (EFFORTS, GREY, GROUPINGS, METRICS, MIN_SHARE, MODEL_SLOTS, SLOT_RAMPS, Group, aggregate,
                          fmt_num, fmt_pct, fmt_usd, model_color, model_label, model_slots, name_color, price_of,
                          row_tokens, segments, total, visible)

OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"


def _h(name):
    """token-meter's projectColor hash, written out again here: h = 7; h = (h*31 + code) mod 2**32."""
    h = 7
    for c in name.lower():
        h = (h * 31 + ord(c)) % 2 ** 32
    return h


def row(project="p", model=OPUS, effort="high", skill="none", device="d1", cost=1.0, input=10, output=20,
        cache_read=300, cache_write=4000, t=0):
    return {"t": t, "model": model, "effort": effort, "project": project, "skill": skill, "device": device,
            "input": input, "output": output, "cache_write": cache_write, "cache_write_1h": 0,
            "cache_read": cache_read, "cost": cost, "premium": False}


def project_rows(costs):
    """One row per project p0, p1, ... with the given costs."""
    return [row(project="p%d" % i, cost=c) for i, c in enumerate(costs)]


# ---------------------------------------------------------------- tables

def test_tables():
    assert METRICS == {"cost": "花费", "tokens": "tokens", "requests": "请求数"}
    assert GROUPINGS == {"model": "按模型", "project": "按项目", "device": "按设备", "skill": "按 skill"}
    assert EFFORTS == ["low", "medium", "high", "xhigh", "max"]
    assert MODEL_SLOTS == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1",
                           "claude-opus-5", "claude-sonnet-5"]
    assert GREY == "#5b5e66"


def test_ramps_are_the_specs_six_by_five():
    assert SLOT_RAMPS == [
        ["#184f95", "#256abf", "#3987e5", "#6da7ec", "#9ec5f4"],
        ["#8a2d00", "#b3400a", "#d95926", "#e68765", "#f2b098"],
        ["#046042", "#037f58", "#199e70", "#68b693", "#9dcfb6"],
        ["#6e4701", "#915f06", "#b57808", "#cb9955", "#ddbb90"],
        ["#4e448c", "#675cb4", "#8276d9", "#9f99e3", "#bebbee"],
        ["#8f2a51", "#b03866", "#d55181", "#e3809f", "#f0abbe"],
    ]
    for ramp in SLOT_RAMPS:
        assert all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in ramp)


# ---------------------------------------------------------------- colours and labels

def test_name_color_matches_token_meter():
    # h = 7; h = (h*31 + ord(c)) mod 2**32; ramp[h % 6][4]
    assert name_color("nasa718") == SLOT_RAMPS[_h("nasa718") % 6][4]
    assert name_color("NASA718") == name_color("nasa718")
    assert name_color("nasa718") == "#9dcfb6"           # h("nasa718") = 1042104278, % 6 == 2: the lightest green
    assert name_color("") == SLOT_RAMPS[7 % 6][4]       # the seed alone
    for name in ("career-ops", "tavily:tavily-research", "macbook-pro", "项目"):
        assert name_color(name) == SLOT_RAMPS[_h(name) % 6][4]


def test_model_colors():
    slots = model_slots(["claude-opus-5-5", "claude-zeta-1", "claude-alpha-1"])
    assert model_color("claude-opus-5-5", "high", slots) == "#3987e5"
    assert model_color("claude-opus-5-5", "low", slots) == "#184f95"
    assert model_color("claude-opus-5-5", "none", slots) == "#5b5e66"
    assert slots["claude-alpha-1"] == 6 % 6 and slots["claude-zeta-1"] == 7 % 6   # spares by name order after the 6 listed
    assert model_color("claude-opus-5-5-20261001", "high", model_slots(["claude-opus-5-5-20261001"])) == "#3987e5"


def test_every_effort_walks_the_ramp():
    slots = model_slots([SONNET])
    assert [model_color(SONNET, e, slots) for e in EFFORTS] == SLOT_RAMPS[1]
    assert model_color(SONNET, "ultra", slots) == GREY      # an effort this module has never heard of


def test_model_slots_for_listed_models():
    assert model_slots(MODEL_SLOTS) == {m: i for i, m in enumerate(MODEL_SLOTS)}
    # input order is irrelevant, and so is who else is present
    assert model_slots([MODEL_SLOTS[3], MODEL_SLOTS[0]]) == {MODEL_SLOTS[3]: 3, MODEL_SLOTS[0]: 0}
    assert model_slots([]) == {}


def test_model_slots_dated_ids_share_their_base_slot():
    slots = model_slots(["claude-opus-5-5-20261001", "claude-opus-5-20260901", "claude-sonnet-5-5"])
    assert slots == {"claude-opus-5-5-20261001": 0, "claude-opus-5-20260901": 4, "claude-sonnet-5-5": 1}
    # a new minor version is not a dated build of a shorter name: it is a different model with a spare slot
    assert model_slots(["claude-opus-5-6"]) == {"claude-opus-5-6": 0}


def test_model_slots_dated_spare_shares_its_base_slot():
    slots = model_slots(["claude-zeta-1", "claude-zeta-1-20261001", "claude-alpha-1"])
    assert slots == {"claude-alpha-1": 0, "claude-zeta-1": 1, "claude-zeta-1-20261001": 1}
    # only the dated build present: it ranks by its base name, here after alpha
    slots = model_slots(["claude-zeta-1-20261001", "claude-alpha-1"])
    assert slots == {"claude-alpha-1": 0, "claude-zeta-1-20261001": 1}
    # a dated build and its base are one spare, not two: the next spare does not move
    assert model_slots(["claude-zeta-1", "claude-zeta-1-20261001", "claude-zz-1"])["claude-zz-1"] == 1
    # only a trailing 8-digit date is a build date
    assert model_slots(["claude-zeta-1", "claude-zeta-1-2026100"]) == {"claude-zeta-1": 0, "claude-zeta-1-2026100": 1}


def test_model_slots_spares_cycle_through_the_six_colours_by_name():
    names = ["claude-m%d" % i for i in range(8)]
    slots = model_slots(list(reversed(names)))
    assert [slots[n] for n in names] == [0, 1, 2, 3, 4, 5, 0, 1]


def test_model_color_for_a_model_missing_from_slots():
    assert model_color(OPUS, "high", {}) == "#3987e5"     # still a listed model
    assert model_color("claude-opus-5-5-20261001", "high", {}) == "#3987e5"
    assert model_color("claude-zeta-1", "high", {}) in [ramp[2] for ramp in SLOT_RAMPS]
    # a dated spare missing from slots still matches its base
    assert model_color("claude-zeta-1-20261001", "high", {}) == model_color("claude-zeta-1", "high", {})


def test_model_label():
    assert model_label("claude-opus-5-5", "xhigh") == "opus-5-5 xhigh"
    assert model_label("claude-sonnet-5-5", "none") == "sonnet-5-5"
    assert model_label("gpt-x", "high") == "gpt-x high"                # no claude- prefix to drop
    assert model_label("None", "none") == "unknown" and model_label("", "high") == "unknown high"
    assert model_label("claude-opus-5-5-20261001", "max") == "opus-5-5-20261001 max"


def test_price_of_is_an_exact_lookup_like_cost_of():
    for model, rates in PRICES.items():
        assert price_of(model) == rates
    assert price_of("claude-opus-5") == PRICES["claude-opus-5"]     # not captured by opus-5-5
    assert price_of("claude-sonnet-5-5") == PRICES["claude-sonnet-5-5"]
    assert price_of("claude-opus-5-5-20261001") is None             # a dated build is unpriced, as in the report
    assert price_of("claude-opus-5-20260901") is None
    assert price_of("claude-opus-5-6") is None and price_of("claude-opus-55") is None
    assert price_of("claude-unknown-9") is None and price_of("None") is None and price_of("") is None


# ---------------------------------------------------------------- formats

def test_formats():
    assert [fmt_usd(x) for x in (97.94, 258.2, 0.2961, 4350, 0)] == ["$97.9", "$258", "$0.296", "$4.35K", "$0"]
    assert [fmt_num(n) for n in (999, 1234, 3_400_000, 5_600_000_000)] == ["999", "1.2K", "3.4M", "5.6B"]


def test_fmt_usd_edges():
    assert [fmt_usd(x) for x in (38.6, 412, 286, 157.3, 5.126, 10, 10.04, 100, 0.0512)] == [
        "$38.6", "$412", "$286", "$157", "$5.13", "$10", "$10", "$100", "$0.0512"]
    assert fmt_usd(0.0000512) == "$0.0000512"                        # tiny stays positional, never 5.12e-05
    assert [fmt_usd(x) for x in (999.6, 999_600, 1000, 1_234_567, 25_000_000)] == [
        "$1K", "$1M", "$1K", "$1.23M", "$25M"]
    assert [fmt_usd(x) for x in (999.4, 12_340, 123_400, 0.9996)] == ["$999", "$12.3K", "$123K", "$1"]
    assert fmt_usd(-4.5) == "-$4.5"


def test_fmt_num_edges():
    assert [fmt_num(n) for n in (0, 1, 1000, 41_200_000, 22_000_000, 999_949, 999_950, 999_950_000)] == [
        "0", "1", "1.0K", "41.2M", "22.0M", "999.9K", "1.0M", "1.0B"]
    assert fmt_num(12.6) == "13" and fmt_num(-1234) == "-1.2K"


def test_fmt_pct():
    assert fmt_pct(0, 0) == "0%" and fmt_pct(5, 0) == "0%" and fmt_pct(0, 100) == "0%"
    assert fmt_pct(71, 100) == "71%" and fmt_pct(100, 100) == "100%" and fmt_pct(1, 100) == "1%"
    assert fmt_pct(0.4, 100) == "0.4%" and fmt_pct(0.96, 100) == "1.0%" and fmt_pct(0.1, 100) == "0.1%"
    assert fmt_pct(0.01, 100) == "<0.1%"                             # would round to 0.0%
    assert fmt_pct(22.4, 100) == "22%" and fmt_pct(1, 3) == "33%"


# ---------------------------------------------------------------- rows and totals

def test_row_tokens_and_group_value():
    r = row(input=1, output=2, cache_read=4, cache_write=8)
    assert row_tokens(r) == 15
    g = Group(key="k", label="k", color=GREY, requests=3, tokens=15, cost=2.5, input=1, output=2, cache_read=4,
              cache_write=8, model=None, effort=None)
    assert (g.members, g.other, g.member_keys) == (1, False, ())
    assert (g.value("cost"), g.value("tokens"), g.value("requests")) == (2.5, 15, 3)
    with pytest.raises(KeyError):
        g.value("nope")


def test_total():
    rows = [row(cost=1.5), row(cost=None), row(cost=0.25, input=1, output=1, cache_read=1, cache_write=1)]
    assert total(rows, "cost") == 1.75
    assert total(rows, "requests") == 3
    assert total(rows, "tokens") == 2 * 4330 + 4


def test_empty_rows():
    assert aggregate([], "project") == [] and segments([], "model", "cost") == [] and total([], "cost") == 0
    assert fmt_pct(0, 0) == "0%"
    for by in GROUPINGS:
        assert aggregate([], by) == [] and segments([], by, "tokens") == []
    for metric in METRICS:
        assert total([], metric) == 0


# ---------------------------------------------------------------- aggregate

def test_aggregate_by_project_largest_cost_first():
    rows = [row(project="small", cost=1), row(project="big", cost=5, input=1, output=2, cache_read=3,
                                                cache_write=4),
            row(project="big", cost=2, input=10, output=20, cache_read=30, cache_write=40),
            row(project="mid", cost=3), row(project="free", cost=None)]
    groups = aggregate(rows, "project")
    assert [g.label for g in groups] == ["big", "mid", "small", "free"]
    big = groups[0]
    assert (big.key, big.requests, big.cost) == ("big", 2, 7)
    assert (big.input, big.output, big.cache_read, big.cache_write, big.tokens) == (11, 22, 33, 44, 110)
    assert big.color == name_color("big") and (big.model, big.effort, big.members, big.other) == (None, None, 1, False)
    assert groups[-1].cost == 0 and groups[-1].requests == 1       # unpriced counts as zero cost, not as missing


def test_aggregate_ties_break_by_name_not_arrival():
    a = aggregate([row(project="b", cost=1), row(project="a", cost=1)], "project")
    b = aggregate([row(project="a", cost=1), row(project="b", cost=1)], "project")
    assert [g.key for g in a] == [g.key for g in b] == ["a", "b"]


def test_aggregate_by_device_and_skill():
    rows = [row(device="mac-mini", skill="investigate", cost=2), row(device="macbook-pro", cost=5),
            row(device="macbook-pro", skill="investigate", cost=1)]
    devices = aggregate(rows, "device")
    assert [(g.key, g.label, g.requests) for g in devices] == [("macbook-pro", "macbook-pro", 2),
                                                               ("mac-mini", "mac-mini", 1)]
    assert devices[0].color == name_color("macbook-pro")
    skills = aggregate(rows, "skill")
    assert [(g.key, g.label, g.cost) for g in skills] == [("none", "无 skill", 5), ("investigate", "investigate", 3)]
    assert skills[0].color == name_color("none") and skills[1].color == name_color("investigate")


def test_aggregate_by_model_key_label_and_colour():
    rows = [row(model=OPUS, effort="xhigh", cost=7), row(model=OPUS, effort="xhigh", cost=1)]
    (g,) = aggregate(rows, "model")
    assert g.key == "claude-opus-5-5\txhigh" and g.label == "opus-5-5 xhigh"
    assert (g.model, g.effort, g.requests, g.cost) == (OPUS, "xhigh", 2, 8)
    assert g.color == SLOT_RAMPS[0][3]


def test_aggregate_by_model_is_slot_order_then_effort_order():
    rows = [row(model="claude-zeta-1", effort="high"), row(model=SONNET, effort="max"),
            row(model=OPUS, effort="none"), row(model=OPUS, effort="xhigh"), row(model=SONNET, effort="low"),
            row(model=OPUS, effort="low"), row(model="claude-alpha-1", effort="medium"),
            row(model="claude-fable-5-1", effort="high"), row(model=OPUS, effort="high")]
    got = [(g.model, g.effort) for g in aggregate(rows, "model")]
    # the six listed models in their listed order, then the others by name; within a model low..max, then none
    assert got == [(OPUS, "low"), (OPUS, "high"), (OPUS, "xhigh"), (OPUS, "none"),
                   (SONNET, "low"), (SONNET, "max"),
                   ("claude-fable-5-1", "high"),
                   ("claude-alpha-1", "medium"),
                   ("claude-zeta-1", "high")]


def test_aggregate_by_model_dated_build_sits_next_to_its_base():
    rows = [row(model="claude-opus-5-5-20261001", effort="high"), row(model=SONNET, effort="high"),
            row(model=OPUS, effort="high")]
    assert [g.model for g in aggregate(rows, "model")] == [OPUS, "claude-opus-5-5-20261001", SONNET]


def test_aggregate_by_model_dated_spare_sits_next_to_its_base_and_shares_its_colour():
    rows = [row(model="claude-zeta-1-20261001", effort="high"), row(model="claude-alpha-1", effort="high"),
            row(model="claude-zeta-1", effort="high"), row(model="claude-zz-1", effort="high")]
    groups = aggregate(rows, "model")
    assert [g.model for g in groups] == ["claude-alpha-1", "claude-zeta-1", "claude-zeta-1-20261001", "claude-zz-1"]
    assert groups[1].color == groups[2].color == SLOT_RAMPS[1][2]


def test_spare_model_keeps_its_colour_on_a_filtered_subset_given_slots_from_all_rows():
    rows = [row(model=OPUS, device="d1"), row(model="claude-alpha-1", device="d1"),
            row(model="claude-zeta-1", device="d2"), row(model="claude-zeta-1", device="d1")]
    slots = model_slots([r["model"] for r in rows])
    assert slots["claude-zeta-1"] == 1
    subset = [r for r in rows if r["device"] == "d2"]           # zeta alone
    (derived,) = aggregate(subset, "model")
    (fixed,) = aggregate(subset, "model", slots)
    assert derived.color == SLOT_RAMPS[0][2]                    # derived from the subset: zeta moves to slot 0
    assert fixed.color == SLOT_RAMPS[1][2] == model_color("claude-zeta-1", "high", slots)
    (seg,) = segments(subset, "model", "cost", slots)
    assert seg.color == fixed.color
    full = {g.model: g.color for g in aggregate(rows, "model")}
    assert full["claude-zeta-1"] == fixed.color                  # the same colour as on the full set
    assert aggregate(subset, "model", slots)[0].key == aggregate(subset, "model")[0].key


def test_aggregate_by_model_unknown_model_and_missing_effort():
    rows = [row(model="None", effort="none", cost=None)]
    (g,) = aggregate(rows, "model")
    assert g.label == "unknown" and g.color == GREY and g.cost == 0 and g.key == "None\tnone"


def test_unknown_model_is_grey_and_does_not_shift_the_spare_slots():
    with_none = model_slots(["None", "claude-zeta-1", "claude-alpha-1"])
    assert with_none["claude-alpha-1"] == 0 and with_none["claude-zeta-1"] == 1
    assert model_color("None", "high", with_none) == GREY and model_color("", "high", {}) == GREY
    (g,) = aggregate([row(model="None", effort="high")], "model")
    assert g.color == GREY and g.label == "unknown high"


def test_aggregate_keeps_every_group_even_tiny_ones():
    rows = project_rows([1000, 0.01])
    assert len(aggregate(rows, "project")) == 2


# ---------------------------------------------------------------- segments

def test_segments_other_rule():
    # 9 projects -> the top 6 plus one grey "其他 · 3 个"
    costs = [90, 80, 70, 60, 50, 40, 30, 20, 10]
    rows = project_rows(costs)
    segs = segments(rows, "project", "cost")
    assert [s.label for s in segs] == ["p0", "p1", "p2", "p3", "p4", "p5", "其他 · 3 个"]
    other = segs[-1]
    assert other.other and other.color == "#5b5e66" and other.members == 3
    assert other.key not in {g.key for g in aggregate(rows, "project")}
    assert other.member_keys == ("p6", "p7", "p8")                 # the merged groups' keys, biggest first
    assert (other.cost, other.requests) == (60, 3)
    assert other.tokens == 3 * (10 + 20 + 300 + 4000) and other.input == 30 and other.output == 60
    assert other.cache_read == 900 and other.cache_write == 12000
    assert (other.model, other.effort) == (None, None)
    assert not any(s.other for s in segs[:-1]) and all(s.members == 1 for s in segs[:-1])
    assert all(s.member_keys == () for s in segs[:-1])
    assert sum(s.cost for s in segs) == total(rows, "cost") == sum(costs)

    # 7 projects -> all 7, no 其他 (a lone leftover is shown as itself)
    segs = segments(project_rows([70, 60, 50, 40, 30, 20, 10]), "project", "cost")
    assert [s.label for s in segs] == ["p0", "p1", "p2", "p3", "p4", "p5", "p6"]
    assert not any(s.other for s in segs)
    assert segs[6].color == name_color("p6")

    # 6 or fewer -> as is
    assert len(segments(project_rows([3, 2, 1]), "project", "cost")) == 3


def test_segments_drop_groups_under_a_tenth_of_a_percent_but_keep_them_in_the_total():
    rows = project_rows([998, 1.5, 0.4, 0.1])             # 0.1% of 1000 is 1: p2 (0.04%) and p3 (0.01%) are under it
    assert total(rows, "cost") == pytest.approx(1000)
    assert [s.label for s in segments(rows, "project", "cost")] == ["p0", "p1"]


def test_segments_a_group_at_exactly_a_tenth_of_a_percent_stays():
    rows = project_rows([999, 1])                          # 1 is exactly 0.1% of 1000
    assert [s.label for s in segments(rows, "project", "cost")] == ["p0", "p1"]


def test_segments_tiny_group_among_seven_is_absent_and_does_not_make_an_other():
    rows = project_rows([70, 60, 50, 40, 30, 20, 0.05])
    segs = segments(rows, "project", "cost")
    assert [s.label for s in segs] == ["p0", "p1", "p2", "p3", "p4", "p5"]
    assert total(rows, "cost") == pytest.approx(270.05)


def test_segments_specks_are_merged_into_other_and_the_segments_add_up_to_the_total():
    # 9 projects, 3 of them specks (0.011% together; costs are dyadic so every sum below is exact)
    costs = [4000, 2000, 1000, 500, 250, 125, 0.5, 0.25, 0.125]
    rows = project_rows(costs)
    segs = segments(rows, "project", "cost")
    assert [s.label for s in segs] == ["p0", "p1", "p2", "p3", "p4", "p5", "其他 · 3 个"]
    other = segs[-1]
    assert other.other and other.members == 3 and other.member_keys == ("p6", "p7", "p8")
    assert other.cost == 0.875 and other.cost / total(rows, "cost") < 0.001      # kept although under 0.1%
    assert sum(s.cost for s in segs) == total(rows, "cost") == sum(costs)
    assert sum(s.requests for s in segs) == total(rows, "requests")
    assert sum(s.tokens for s in segs) == total(rows, "tokens")


def test_segments_a_lone_leftover_that_is_a_speck_is_dropped_but_two_make_an_other():
    # 7 projects, the 7th a speck: shown as itself, then dropped as under 0.1%
    segs = segments(project_rows([70, 60, 50, 40, 30, 20, 0.01]), "project", "cost")
    assert [s.label for s in segs] == ["p0", "p1", "p2", "p3", "p4", "p5"]
    # 8 projects, the 8th a speck: the rest is two groups, so they merge, speck included
    segs = segments(project_rows([70, 60, 50, 40, 30, 20, 10, 0.01]), "project", "cost")
    assert [s.label for s in segs] == ["p0", "p1", "p2", "p3", "p4", "p5", "其他 · 2 个"]
    assert segs[-1].cost == pytest.approx(10.01) and segs[-1].member_keys == ("p6", "p7")
    # the rest is one big group and one speck: still a 其他, with both in it
    segs = segments(project_rows([70, 60, 50, 40, 30, 20, 10, 9, 0.01, 0.02]), "project", "cost")
    assert segs[-1].label == "其他 · 4 个" and segs[-1].member_keys == ("p6", "p7", "p9", "p8")
    assert segs[-1].cost == pytest.approx(19.03) and len(segs) == 7


def test_segments_ignore_groups_with_none_of_the_metric_when_merging():
    costs = [70, 60, 50, 40, 30, 20, 10, None, None]            # two projects that only used unpriced models
    rows = [row(project="p%d" % i, cost=c) for i, c in enumerate(costs)]
    segs = segments(rows, "project", "cost")
    assert [s.label for s in segs] == ["p0", "p1", "p2", "p3", "p4", "p5", "p6"] and not any(s.other for s in segs)
    assert segments(rows, "project", "requests")[-1].label == "其他 · 3 个"


def test_visible_and_min_share():
    assert MIN_SHARE == 0.001
    groups = aggregate(project_rows([999, 1, 0.5, 0]), "project")
    assert [g.label for g in visible(groups, "cost", 1000.5)] == ["p0"]            # 1 is just under 0.1% of 1000.5
    assert [g.label for g in visible(groups, "cost", 1000)] == ["p0", "p1"]        # exactly 0.1% stays; 0.5 and 0 go
    assert [g.label for g in visible(groups, "requests", 4)] == ["p0", "p1", "p2", "p3"]
    assert visible([], "cost", 0) == []
    other = Group(key="__other__", label="其他 · 2 个", color=GREY, requests=2, tokens=2, cost=0.2, input=0, output=0,
                  cache_read=0, cache_write=0, model=None, effort=None, members=2, other=True,
                  member_keys=("a", "b"))
    assert visible([other], "cost", 1000) == [other]                              # 其他 is exempt from the 0.1%
    assert visible([other], "cost", 0.1) == [other]
    assert visible([groups[2]], "cost", 1000) == []


def test_segments_rank_by_the_chosen_metric_not_by_cost():
    rows = [row(project="costly", cost=100, input=500, output=0, cache_read=0, cache_write=0)]
    rows += [row(project="tokens%d" % i, cost=1, input=1000 * (i + 1), output=0, cache_read=0, cache_write=0)
             for i in range(6)]
    by_cost = segments(rows, "project", "cost")
    assert by_cost[0].label == "costly" and len(by_cost) == 7
    by_tokens = segments(rows, "project", "tokens")
    assert [s.label for s in by_tokens[:2]] == ["tokens5", "tokens4"] and by_tokens[-1].label == "costly"
    by_requests = segments(rows, "project", "requests")
    assert len(by_requests) == 7 and all(s.requests == 1 for s in by_requests)


def test_segments_by_model_are_all_of_them_in_slot_order():
    efforts = ["low", "medium", "high", "xhigh", "max"]
    rows = [row(model=m, effort=e, cost=10 + i) for m in (OPUS, SONNET, HAIKU) for i, e in enumerate(efforts)]
    segs = segments(rows, "model", "cost")
    assert len(segs) == 15 and not any(s.other for s in segs)          # no 6-limit, no 其他
    assert [(s.model, s.effort) for s in segs] == [(g.model, g.effort) for g in aggregate(rows, "model")]
    assert segs[0].label == "opus-5-5 low" and segs[-1].label == "haiku-5-5 max"


def test_segments_by_model_drop_tiny_ones_too():
    rows = [row(model=OPUS, effort="high", cost=1000), row(model=HAIKU, effort="low", cost=0.1)]
    assert [s.label for s in segments(rows, "model", "cost")] == ["opus-5-5 high"]
    assert [s.label for s in segments(rows, "model", "requests")] == ["opus-5-5 high", "haiku-5-5 low"]


def test_segments_by_device_and_skill_merge_too():
    rows = [row(device="dev%d" % i, skill="skill%d" % i, cost=10 - i) for i in range(8)]
    for by in ("device", "skill"):
        segs = segments(rows, by, "cost")
        assert len(segs) == 7 and segs[-1].label == "其他 · 2 个" and segs[-1].members == 2


def test_segments_with_no_value_in_the_metric():
    rows = [row(project="a", cost=None), row(project="b", cost=None)]
    assert segments(rows, "project", "cost") == []         # nothing to draw, nothing to divide by
    assert [s.label for s in segments(rows, "project", "requests")] == ["a", "b"]


def test_segments_key_of_other_cannot_collide_with_a_project():
    rows = project_rows([70, 60, 50, 40, 30, 20, 10, 5]) + [row(project="其他", cost=100)]
    segs = segments(rows, "project", "cost")
    assert len({s.key for s in segs}) == len(segs)
