"""Metrics, grouping, fixed colours and number formats for the billing-usage TUI.

Pure standard library, no I/O. A row is one request as billing_usage.request_rows() emits it, plus the
"device" the TUI adds: {"t", "model", "effort", "project", "skill", "device", "input", "output", "cache_write",
"cache_write_1h", "cache_read", "cost" (float or None), "premium"}.

The share table lists aggregate()'s groups; the timeline and its legend draw segments(), which merges the long
tail into one grey "其他" and drops groups too small to see. Colours are fixed per name, never per rank.
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
MIN_SHARE_DIV = 1000  # segments() drops groups under 1/1000 (0.1%) of the total
OTHER_KEY = "__other__"  # Group.key of the merged "其他"; no project, device or skill is named like this
_DATE_SUFFIX = re.compile(r"\d{8}$")


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
    members: int = 1  # how many groups were merged into this one
    other: bool = False  # the merged "其他"

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

def _base_model(model: str) -> Optional[str]:
    """The listed model `model` is, or a dated build of (`<listed>-YYYYMMDD`); None for any other model."""
    best = None
    for listed in MODEL_SLOTS:
        if model == listed or (model.startswith(listed + "-") and _DATE_SUFFIX.fullmatch(model[len(listed) + 1:])):
            if best is None or len(listed) > len(best):
                best = listed
    return best


def _unknown(model: Optional[str]) -> bool:
    return not model or model == "None"  # billing_usage writes a missing model as "None"


def _model_ranks(models: Iterable[str]) -> Dict[str, int]:
    """Colour-assignment order: the six listed models first, then the rest by name, unknown last."""
    models = set(models)
    ranks = {}
    for m in models:
        base = _base_model(m)
        if base is not None:
            ranks[m] = MODEL_SLOTS.index(base)
    spares = sorted(m for m in models if m not in ranks and not _unknown(m))
    for i, m in enumerate(spares):
        ranks[m] = len(MODEL_SLOTS) + i
    for m in models:
        if m not in ranks:
            ranks[m] = len(MODEL_SLOTS) + len(spares)
    return ranks


def model_slots(models: List[str]) -> Dict[str, int]:
    """Colour slot (index into SLOT_RAMPS) per model. A dated build shares its base model's slot; the other
    models, by name order, take the slots after the listed six, wrapping round."""
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
    if slot is None:  # not in `slots`: the same answer model_slots() gives for the listed models
        base = _base_model(model)
        slot = MODEL_SLOTS.index(base) if base is not None else _name_hash(model)
    return SLOT_RAMPS[slot % len(SLOT_RAMPS)][EFFORTS.index(effort)]


def model_label(model: Optional[str], effort: str) -> str:
    """`opus-5-5 xhigh`: no `claude-` prefix, and no effort when there is none."""
    name = "unknown" if _unknown(model) else (model[len("claude-"):] if model.startswith("claude-") else model)
    return name if effort == "none" else "%s %s" % (name, effort)


def price_of(model: str) -> Optional[Tuple[float, ...]]:
    """(input, 5m write, 1h write, read, output) USD per million tokens: the model's own entry in PRICES, else
    that of the longest PRICES key it starts with (followed by `-`, as a dated build does); None if unpriced."""
    if model in PRICES:
        return PRICES[model]
    keys = [k for k in PRICES if model.startswith(k + "-")]
    return PRICES[max(keys, key=len)] if keys else None


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


def aggregate(rows: List[dict], by: str) -> List[Group]:
    """One Group per distinct key, none dropped or merged. By model the key is (model, effort), in colour-slot
    order and then effort order; otherwise the group is the project, device or skill, largest cost first."""
    if by not in GROUPINGS:
        raise ValueError("unknown grouping %r" % (by,))
    groups = {}  # type: Dict[Tuple[str, ...], Group]
    if by == "model":
        ranks = _model_ranks({str(r["model"]) for r in rows})
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
    other.members, other.other = len(groups), True
    for g in groups:
        other.requests += g.requests
        other.tokens += g.tokens
        other.cost += g.cost
        other.input += g.input
        other.output += g.output
        other.cache_read += g.cache_read
        other.cache_write += g.cache_write
    return other


def segments(rows: List[dict], by: str, metric: str) -> List[Group]:
    """The groups the timeline and legend draw. Groups under 0.1% of the metric are left out (total() still
    counts them). By model every other group stays; otherwise the top 6 by `metric` stay and the rest merge
    into one grey "其他 · N 个" - unless the rest is a single group, which then shows as itself."""
    whole = total(rows, metric)
    kept = [g for g in aggregate(rows, by) if g.value(metric) > 0 and g.value(metric) * MIN_SHARE_DIV >= whole]
    if by == "model":
        return kept
    kept.sort(key=lambda g: -g.value(metric))  # stable: aggregate()'s order breaks ties
    top, rest = kept[:TOP_N], kept[TOP_N:]
    if len(rest) == 1:
        return top + rest
    return top + [_merge(rest)] if rest else top


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
