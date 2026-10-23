"""Metrics, grouping, fixed colours and number formats for the billing-usage TUI.

Pure standard library, no I/O. A row is one request as billing_usage.request_rows() emits it, plus the
"device" the TUI adds: {"t", "model", "effort", "project", "skill", "device", "input", "output", "cache_write",
"cache_write_1h", "cache_read", "cost" (float or None), "premium"}.

The share table lists visible(aggregate(...)) and greys the rows named in the "其他" group's member_keys; the
timeline and its legend draw segments(), which merges the long tail into one grey "其他" and drops groups too
small to see. Colours are fixed per name, never per rank; pass the same `slots` (from model_slots() over all rows)
to every aggregate()/segments() call so a model keeps its colour when the rows are filtered.
"""
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from billing_usage import PRICES

METRICS = {"cost": "花费", "tokens": "tokens", "requests": "请求数"}
GROUPINGS = {"model": "按模型", "project": "按项目", "device": "按设备", "skill": "按 skill"}

# Six colour slots, five shades each, lightest last. The shades follow EFFORTS; the middle one (high) is the
# slot's own colour. Values are token-meter's extension/media/common.js.
SLOT_RAMPS = [
    ["#184f95", "#256abf", "#3987e5", "#6da7ec", "#9ec5f4"],  # 0 blue
    ["#8a2d00", "#b3400a", "#d95926", "#e68765", "#f2b098"],  # 1 orange
    ["#046042", "#037f58", "#199e70", "#68b693", "#9dcfb6"],  # 2 green
    ["#6e4701", "#915f06", "#b57808", "#cb9955", "#ddbb90"],  # 3 gold
    ["#4e448c", "#675cb4", "#8276d9", "#9f99e3", "#bebbee"],  # 4 purple
    ["#8f2a51", "#b03866", "#d55181", "#e3809f", "#f0abbe"],  # 5 rose
]  # type: List[List[str]]
GREY = "#5b5e66"  # no effort, and the merged "其他"
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
# Models that own slots 0-5 in this order. Any other model takes a later slot, by name order.
MODEL_SLOTS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1", "claude-opus-5",
               "claude-sonnet-5"]

TOP_N = 6  # groups segments() keeps before merging the rest into "其他"
MIN_SHARE_DIV = 1000
MIN_SHARE = 1 / MIN_SHARE_DIV  # groups under this share (0.1%) of the total are not listed; visible() applies it
OTHER_KEY = "__other__"  # Group.key of the merged "其他"; no project, device or skill is named like this
_DATE_SUFFIX = re.compile(r"(?<=.)-\d{8}$")  # a dated build: claude-opus-5-5-20261001


# ---------------------------------------------------------------- rows

def row_tokens(r: dict) -> int:
    """Every token the request moved: input + output + cache read + cache write."""
    return r["input"] + r["output"] + r["cache_read"] + r["cache_write"]


@dataclass
class Group:
    key: str  # stable id; by model "<model>\t<effort>", otherwise the name
    label: str
    color: str
    requests: int
    tokens: int
    cost: float
    input: int
    output: int
    cache_read: int
    cache_write: int
    model: Optional[str]
    effort: Optional[str]
    members: int = 1  # the merged "其他": how many groups it holds (len(member_keys))
    other: bool = False  # the merged "其他"
    member_keys: Tuple[str, ...] = ()  # the merged "其他": Group.key of every group in it; () on any other group

    def value(self, metric: str) -> float:
        return {"cost": self.cost, "tokens": self.tokens, "requests": self.requests}[metric]


def total(rows: Iterable[dict], metric: str) -> float:
    """The metric summed over every row; unpriced rows add no cost."""
    if metric == "cost":
        return float(sum(r["cost"] or 0.0 for r in rows))
    if metric == "tokens":
        return float(sum(row_tokens(r) for r in rows))
    if metric == "requests":
        return float(sum(1 for _ in rows))
    raise KeyError(metric)


# ---------------------------------------------------------------- models, colours, prices

def _strip_date(model: str) -> str:
    """The model without a `-YYYYMMDD` build date: claude-opus-5-5-20261001 -> claude-opus-5-5."""
    return _DATE_SUFFIX.sub("", model)


def _unknown(model: Optional[str]) -> bool:
    return not model or model == "None"  # billing_usage writes a missing model as "None"


def _model_ranks(models: Iterable[str]) -> Dict[str, int]:
    """Colour-assignment order, by base name (a dated build ranks with its base): the six listed models in their
    listed order, then the others by name, an unknown model last."""
    models = set(models)
    spares = sorted({_strip_date(m) for m in models if _strip_date(m) not in MODEL_SLOTS and not _unknown(m)})
    ranks = {}
    for m in models:
        base = _strip_date(m)
        if base in MODEL_SLOTS:
            ranks[m] = MODEL_SLOTS.index(base)
        elif _unknown(m):
            ranks[m] = len(MODEL_SLOTS) + len(spares)
        else:
            ranks[m] = len(MODEL_SLOTS) + spares.index(base)
    return ranks


def model_slots(models: List[str]) -> Dict[str, int]:
    """Colour slot (index into SLOT_RAMPS) per model. A dated build shares its base model's slot; models other
    than the listed six take the slots after them, by base-name order, wrapping round."""
    return {m: rank % len(SLOT_RAMPS) for m, rank in _model_ranks(models).items()}


def _name_hash(name: str) -> int:
    """token-meter's projectColor hash, case-insensitive: h = 7; h = (h*31 + code) mod 2**32."""
    h = 7
    for c in name.lower():
        h = (h * 31 + ord(c)) % 2 ** 32
    return h


def name_color(name: str) -> str:
    """A project's, device's or skill's colour: the lightest shade of the slot its name hashes to."""
    return SLOT_RAMPS[_name_hash(name) % len(SLOT_RAMPS)][-1]


def model_color(model: str, effort: str, slots: Dict[str, int]) -> str:
    """The model's slot ramp at the effort's shade; grey without a known effort (or without a model)."""
    if effort not in EFFORTS or _unknown(model):
        return GREY
    slot = slots.get(model)
    if slot is None:  # not in `slots`: the listed models keep theirs, any other gets a stable one from its name
        base = _strip_date(model)
        slot = MODEL_SLOTS.index(base) if base in MODEL_SLOTS else _name_hash(base)
    return SLOT_RAMPS[slot % len(SLOT_RAMPS)][EFFORTS.index(effort)]


def model_label(model: Optional[str], effort: str) -> str:
    """`opus-5-5 xhigh`: no `claude-` prefix, and no effort when there is none."""
    name = "unknown" if _unknown(model) else (model[len("claude-"):] if model.startswith("claude-") else model)
    return name if effort == "none" else "%s %s" % (name, effort)


def price_of(model: str) -> Optional[Tuple[float, ...]]:
    """(input, 5m write, 1h write, read, output) USD per million tokens: the exact PRICES entry, as
    billing_usage.cost_of looks it up, so a dated build has none (the report shows it unpriced, cost None)."""
    return PRICES.get(model)


# ---------------------------------------------------------------- grouping

def _effort_order(effort: str) -> int:
    return EFFORTS.index(effort) if effort in EFFORTS else len(EFFORTS)


def _empty(key: str, label: str, color: str, model: Optional[str], effort: Optional[str]) -> Group:
    return Group(key=key, label=label, color=color, requests=0, tokens=0, cost=0.0, input=0, output=0,
                 cache_read=0, cache_write=0, model=model, effort=effort)


def _add(group: Group, r: dict) -> None:
    group.requests += 1
    group.tokens += row_tokens(r)
    group.cost += r["cost"] or 0.0
    group.input += r["input"]
    group.output += r["output"]
    group.cache_read += r["cache_read"]
    group.cache_write += r["cache_write"]


def aggregate(rows: List[dict], by: str, slots: Optional[Dict[str, int]] = None) -> List[Group]:
    """One Group per distinct key, none dropped or merged. By model the key is (model, effort), in colour-slot
    order and then effort order; otherwise the group is the project, device or skill, largest cost first.
    `slots` (model_slots() over all the rows, not just these) fixes the model colours; None derives them from
    `rows`, which recolours the non-listed models when `rows` is a subset."""
    if by not in GROUPINGS:
        raise ValueError("unknown grouping %r" % (by,))
    groups = {}  # type: Dict[Tuple[str, ...], Group]
    if by == "model":
        ranks = _model_ranks({str(r["model"]) for r in rows})
        if slots is None:
            slots = {m: rank % len(SLOT_RAMPS) for m, rank in ranks.items()}
        for r in rows:
            model, effort = str(r["model"]), r["effort"]
            if (model, effort) not in groups:
                groups[(model, effort)] = _empty("%s\t%s" % (model, effort), model_label(model, effort),
                                                 model_color(model, effort, slots), model, effort)
            _add(groups[(model, effort)], r)
        return sorted(groups.values(), key=lambda g: (ranks[g.model], g.model, _effort_order(g.effort), g.effort))
    missing = "none" if by == "skill" else "unknown"
    for r in rows:
        name = r.get(by) or missing
        if (name,) not in groups:
            groups[(name,)] = _empty(name, "无 skill" if by == "skill" and name == "none" else name,
                                     name_color(name), None, None)
        _add(groups[(name,)], r)
    return sorted(groups.values(), key=lambda g: (-g.cost, g.key))


def _merge(groups: List[Group]) -> Group:
    other = _empty(OTHER_KEY, "其他 · %d 个" % len(groups), GREY, None, None)
    other.other = True
    other.member_keys = tuple(g.key for g in groups)
    other.members = len(other.member_keys)
    for g in groups:
        other.requests += g.requests
        other.tokens += g.tokens
        other.cost += g.cost
        other.input += g.input
        other.output += g.output
        other.cache_read += g.cache_read
        other.cache_write += g.cache_write
    return other


def visible(groups: List[Group], metric: str, whole: float) -> List[Group]:
    """The groups worth listing: those with some of `metric` and at least 0.1% of `whole` (the metric's total
    over all rows, which the dropped groups still count in). The merged "其他" is exempt from the 0.1%."""
    return [g for g in groups
            if g.value(metric) > 0 and (g.other or g.value(metric) * MIN_SHARE_DIV >= whole)]


def segments(rows: List[dict], by: str, metric: str, slots: Optional[Dict[str, int]] = None) -> List[Group]:
    """The groups the timeline and legend draw. By model: every group with 0.1% or more of the metric. Otherwise
    the groups are ranked by `metric`, the top 6 stay and the rest - small ones included - merge into one grey
    "其他 · N 个" (member_keys says which), unless the rest is a single group, which then shows as itself;
    then any group other than "其他" under 0.1% of total() is dropped. Groups with none of the metric are ignored.
    So the segments add up to total() except for what was dropped: by model any group under 0.1%, otherwise
    only a group under 0.1% that was left over on its own or ranked in the top 6."""
    whole = total(rows, metric)
    groups = [g for g in aggregate(rows, by, slots) if g.value(metric) > 0]
    if by != "model":
        groups.sort(key=lambda g: -g.value(metric))  # stable: aggregate()'s order breaks ties
        top, rest = groups[:TOP_N], groups[TOP_N:]
        if len(rest) == 1:
            top = top + rest
        elif rest:
            top = top + [_merge(rest)]
        groups = top
    return visible(groups, metric, whole)


# ---------------------------------------------------------------- number formats

def fmt_usd(x: float) -> str:
    """Three significant digits without trailing zeros, K/M above a thousand: $97.9 $258 $0.296 $4.35K."""
    sign, x = ("-" if x < 0 else ""), abs(x)
    if x == 0:
        return "$0"
    suffix = ""
    rounded = float("%.3g" % x)  # a value that rounds up to 1000 belongs to the next unit
    if rounded >= 1e6:
        x, suffix = x / 1e6, "M"
    elif rounded >= 1e3:
        x, suffix = x / 1e3, "K"
    exponent = int(("%.2e" % x).split("e")[1])  # of the rounded value, so 9.996 counts as 1.00e+01
    text = "%.*f" % (max(0, 2 - exponent), x)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "%s$%s%s" % (sign, text, suffix)


def fmt_num(n: float) -> str:
    """Whole numbers below a thousand, then one decimal with K/M/B: 999 1.2K 3.4M 5.6B."""
    sign, n = ("-" if n < 0 else ""), int(round(abs(n)))
    if n < 1000:
        return "%s%d" % (sign, n)
    for divisor, suffix in ((10 ** 3, "K"), (10 ** 6, "M"), (10 ** 9, "B")):
        tenths = (n * 10 + divisor // 2) // divisor  # rounded half up, exact in integers
        if tenths < 10000 or suffix == "B":  # 1000.0K is 1.0M
            break
    return "%s%d.%d%s" % (sign, tenths // 10, tenths % 10, suffix)


def fmt_pct(part: float, whole: float) -> str:
    """A whole percent from 1% up, one decimal below; 0% for nothing, <0.1% for a speck."""
    if not part or not whole:
        return "0%"
    pct = part * 100 / whole
    if pct >= 1:
        return "%.0f%%" % pct
    if pct < 0.05:
        return "<0.1%"
    return "%.1f%%" % pct
