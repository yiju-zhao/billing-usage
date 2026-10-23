"""Widgets for the billing-usage TUI: the sentence pickers, the share table, the custom-date row and the
dialogs. The timeline lives in tui_timeline.

Text that is not data is a quiet grey; the words you can change are brighter and underlined; numbers are
right-aligned in fixed columns. Widths are counted in terminal cells (CJK characters take two).
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Region
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from usage_groups import GREY, Group, fmt_num, fmt_pct, fmt_usd, price_of

# Panel palette: a dark sidebar, quiet text, brighter choosable words.
BG = "#17181c"
SURFACE = "#1f2126"
RULE = "#2b2d33"
MUTED = "#80848d"
TEXT = "#c9ccd2"
BRIGHT = "#e8eaee"
GREY_TEXT = "#6b6e76"  # a row merged into 其他 elsewhere: its name and numbers
WARN = "#cb9955"  # gold ramp, light
ALERT = "#e3809f"  # rose ramp, light
TRACK_MIX = 0.72  # how far the share bar's empty part fades from the group colour towards BG

BAR_CELLS = 18
MIN_BAR = 6
MIN_NAME = 6
GAP = 2
NAME_HEADS = {"model": "模型", "project": "项目", "device": "设备", "skill": "skill"}

Choice = Tuple  # (value, label) or (value, label, note); a note disables the option and says why


def fmt_metric(value: float, metric: str) -> str:
    """A metric value as the sentence totals and the table show it."""
    if metric == "cost":
        return fmt_usd(value)
    if metric == "tokens":
        return fmt_num(value)
    return f"{int(round(value)):,}"


def blend(color: str, toward: str, amount: float) -> str:
    """`color` moved `amount` (0-1) of the way to `toward`; both `#rrggbb`."""
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(toward[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join("%02x" % round(x + (y - x) * amount) for x, y in zip(a, b))


def _fit(text: str, width: int) -> str:
    """`text` cut to `width` cells, with `~` marking a cut."""
    if cell_len(text) <= width:
        return text
    out, used = "", 0
    for ch in text:
        w = cell_len(ch)
        if used + w > width - 1:
            break
        out, used = out + ch, used + w
    return out + "~"


def _pad(text: str, width: int, right: bool = False) -> str:
    space = " " * max(0, width - cell_len(text))
    return space + text if right else text + space


def _rate(x: float) -> str:
    return "%g" % x


# ---------------------------------------------------------------- the share table

def share_table_lines(groups: Sequence[Group], metric: str, by: str, whole: float, grey: Iterable[str] = (),
                      width: int = 100) -> Tuple[Text, List[Text]]:
    """The header and one line per group, sorted by `metric`, largest first, fitted to `width` cells.

    Columns: name, an 18-cell share bar, %, 花费, 请求, tokens; by model also the input/output and cache
    read/write (1 h) prices per million. When the width is short the price columns go first, then the bar
    narrows, then the name. Groups whose key is in `grey` (merged into 其他 in the timeline) are drawn grey.
    """
    grey = set(grey)
    groups = sorted(groups, key=lambda g: -g.value(metric))
    names = [g.label for g in groups]
    pcts = [fmt_pct(g.value(metric), whole) for g in groups]
    costs = [fmt_usd(g.cost) for g in groups]
    reqs = [f"{g.requests:,}" for g in groups]
    toks = [fmt_num(g.tokens) for g in groups]
    io_prices, cache_prices = [], []
    for g in groups:
        p = price_of(g.model) if g.model else None
        io_prices.append("—" if p is None else "%s/%s" % (_rate(p[0]), _rate(p[4])))
        cache_prices.append("—" if p is None else "%s/%s" % (_rate(p[3]), _rate(p[2])))

    def col_w(head, values):
        return max([cell_len(head)] + [cell_len(v) for v in values])

    name_head = NAME_HEADS.get(by, "名称")
    numeric = [("花费", costs), ("请求", reqs), ("tokens", toks)]
    prices = [("输入/输出 $/M", io_prices), ("缓存读/写 $/M", cache_prices)] if by == "model" else []
    pct_w = max(4, max([cell_len(p) for p in pcts] or [0]))
    name_natural = col_w(name_head, names)

    def fixed(cols, bar):
        return bar + 1 + pct_w + sum(GAP + col_w(h, v) for h, v in cols)

    cols, bar_w = numeric + prices, BAR_CELLS
    if prices and fixed(cols, bar_w) + GAP + MIN_NAME > width:
        cols = numeric
    if fixed(cols, bar_w) + GAP + MIN_NAME > width:
        bar_w = max(MIN_BAR, width - GAP - MIN_NAME - fixed(cols, 0))
    name_w = max(MIN_NAME, min(name_natural, width - GAP - fixed(cols, bar_w)))
    widths = [col_w(h, v) for h, v in cols]

    head = Text(style=MUTED)
    head.append(_pad(_fit(name_head, name_w), name_w) + " " * GAP)
    head.append(_pad("占比", bar_w + 1 + pct_w))
    for (h, _), w in zip(cols, widths):
        head.append(" " * GAP + _pad(h, w, right=True))

    lines = []
    for i, g in enumerate(groups):
        is_grey = g.key in grey or g.other
        color = GREY if is_grey else g.color
        fg = GREY_TEXT if is_grey else TEXT
        share = g.value(metric) / whole if whole else 0.0
        filled = min(bar_w, max(1 if share > 0 else 0, round(share * bar_w)))
        line = Text()
        line.append(_pad(_fit(names[i], name_w), name_w), style=GREY_TEXT if is_grey else BRIGHT)
        line.append(" " * GAP)
        line.append("█" * filled, style=color)
        line.append("░" * (bar_w - filled), style=blend(color, BG, TRACK_MIX))
        line.append(" " + _pad(pcts[i], pct_w, right=True), style=fg)
        for (_, values), w in zip(cols, widths):
            line.append(" " * GAP + _pad(values[i], w, right=True), style=fg)
        lines.append(line)
    return head, lines


class TableHead(Widget):
    def __init__(self, table: "ShareTable") -> None:
        super().__init__()
        self.table = table

    def render(self) -> Text:
        return self.table._head if self.table._head is not None else Text("")


class TableBody(Widget):
    def __init__(self, table: "ShareTable") -> None:
        super().__init__()
        self.table = table

    def get_content_height(self, container, viewport, width) -> int:
        return max(1, len(self.table._lines))

    def render(self) -> Text:
        if not self.table._lines:
            return Text(self.table._empty, style=MUTED)
        return Text("\n").join(self.table._lines)


class ShareTable(Vertical):
    """One row per group with a share bar; the body scrolls, the header stays."""

    DEFAULT_CSS = f"""
    ShareTable {{ height: 1fr; padding: 0 1; }}
    ShareTable > Vertical {{ height: 1fr; }}
    ShareTable TableHead {{ height: 1; }}
    ShareTable VerticalScroll {{
        height: 1fr; scrollbar-size-vertical: 1; scrollbar-background: {BG}; scrollbar-background-hover: {BG};
        scrollbar-background-active: {BG}; scrollbar-color: #3a3d45; scrollbar-color-hover: #4a4e58;
        scrollbar-color-active: #5b5e66; scrollbar-corner-color: {BG};
    }}
    ShareTable TableBody {{ height: auto; }}
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._data = ([], "cost", "project", 0.0, frozenset())
        self._empty = "无数据"
        self._head: Optional[Text] = None
        self._lines: List[Text] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield TableHead(self)
            with VerticalScroll():
                yield TableBody(self)

    def update(self, groups: Sequence[Group], metric: str, by: str, total: float, grey: Iterable[str] = (),
               empty: str = "无数据") -> None:
        self._data = (list(groups), metric, by, total, frozenset(grey))
        self._empty = empty
        self._layout_lines()

    def _layout_lines(self) -> None:
        groups, metric, by, total, grey = self._data
        width = max(20, (self.content_size.width or 102) - 2)  # leave the scrollbar its cell and a gap
        if groups:
            self._head, self._lines = share_table_lines(groups, metric, by, total, grey, width)
        else:
            self._head, self._lines = None, []
        for child in self.query("TableHead, TableBody"):
            child.refresh(layout=True)

    def on_resize(self, event: events.Resize) -> None:
        self._layout_lines()


# ---------------------------------------------------------------- sentences

class Word(Static, can_focus=True):
    """One choosable word in a sentence."""

    BINDINGS = [Binding("enter,space", "choose", "选择", show=False)]
    DEFAULT_CSS = f"""
    Word {{ width: auto; height: 1; color: {BRIGHT}; text-style: underline; }}
    Word:hover {{ color: #ffffff; background: {SURFACE}; }}
    Word:focus {{ color: #ffffff; background: #2d3038; text-style: underline; }}
    """

    def __init__(self, field: str, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.field = field

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.action_choose()

    def action_choose(self) -> None:
        self.parent.open_choices(self)


class SentenceBar(Horizontal):
    """`花费 · 所有设备 · 按模型 · ...` with the section's total on the right. Choosing a word posts Changed."""

    DEFAULT_CSS = f"""
    SentenceBar {{ height: 1; padding: 0 1; color: {MUTED}; overflow: hidden hidden; }}
    SentenceBar > .sep {{ width: auto; color: {MUTED}; }}
    SentenceBar > .total {{ width: 1fr; min-width: 8; text-align: right; color: {BRIGHT}; }}
    """

    class Changed(Message):
        def __init__(self, section: str, field: str, value: str) -> None:
            super().__init__()
            self.section, self.field, self.value = section, field, value

    def __init__(self, section: str, fields: List[Tuple[str, List[Choice]]], values: Optional[dict] = None,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.section = section
        self._fields = [name for name, _ in fields]
        self._options = {name: list(options) for name, options in fields}
        self._values = {name: (values or {}).get(name, options[0][0] if options else "")
                        for name, options in fields}
        self._words = {name: Word(name, id=f"{section}-{name}") for name in self._fields}
        self._overrides: dict = {}  # field -> label shown instead of the chosen option's (the zoomed bucket)

    def compose(self) -> ComposeResult:
        for i, name in enumerate(self._fields):
            if i:
                yield Static(" · ", classes="sep")
            yield self._words[name]
        yield Static("", classes="total")

    def on_mount(self) -> None:
        for name in self._fields:
            self._show(name)

    def word(self, field: str) -> Word:
        return self._words[field]

    def value(self, field: str) -> str:
        return self._values[field]

    def label(self, field: str) -> str:
        for choice in self._options[field]:
            if choice[0] == self._values[field]:
                return choice[1]
        return self._values[field]

    def _show(self, field: str) -> None:
        self._words[field].update(self._overrides.get(field) or self.label(field))

    def set_override(self, field: str, label: Optional[str]) -> None:
        """Show `label` for `field` without changing its value; None shows the chosen option again."""
        if self._overrides.get(field) != label:
            if label is None:
                self._overrides.pop(field, None)
            else:
                self._overrides[field] = label
            self._show(field)

    def set_value(self, field: str, value: str) -> None:
        self._values[field] = value
        self._show(field)

    def set_options(self, field: str, options: List[Choice]) -> None:
        self._options[field] = list(options)
        self._show(field)

    def set_total(self, text: str) -> None:
        if self.is_mounted:
            self.query_one(".total", Static).update(text)

    def open_choices(self, word: Word) -> None:
        field = word.field

        def chosen(value: Optional[str]) -> None:
            if value is not None and value != self._values[field]:
                self.set_value(field, value)
                self.post_message(self.Changed(self.section, field, value))
            word.focus()

        self.app.push_screen(ChoiceScreen(self._options[field], self._values[field], word.region), chosen)


class ChoiceScreen(ModalScreen):
    """The choices for one word, dropped down under it."""

    BINDINGS = [Binding("escape", "dismiss_none", "关闭", show=False)]
    DEFAULT_CSS = f"""
    ChoiceScreen {{ align: left top; }}
    ChoiceScreen > OptionList {{
        width: auto; min-width: 16; max-width: 64; height: auto; max-height: 16;
        background: {SURFACE}; border: round #3a3d45; padding: 0 0; color: {TEXT};
    }}
    ChoiceScreen > OptionList:focus {{ border: round #4a4e58; background-tint: {BG} 0%; }}
    ChoiceScreen > OptionList > .option-list--option {{ padding: 0 1; }}
    ChoiceScreen > OptionList > .option-list--option-highlighted,
    ChoiceScreen > OptionList:focus > .option-list--option-highlighted {{
        background: #33373f; color: #ffffff; text-style: none;
    }}
    ChoiceScreen > OptionList > .option-list--option-hover {{ background: #2a2d34; }}
    """

    def __init__(self, options: List[Choice], current: str, anchor: Region) -> None:
        super().__init__(classes="-dialog")
        self._choices, self._current, self._anchor = options, current, anchor

    def compose(self) -> ComposeResult:
        items = []
        for choice in self._choices:
            value, label = choice[0], choice[1]
            note = choice[2] if len(choice) > 2 else None
            prompt = Text(label, style=BRIGHT if value == self._current else TEXT)
            if note:
                prompt = Text(label, style=GREY_TEXT) + Text(note, style=GREY_TEXT)
            items.append(Option(prompt, id=value, disabled=bool(note)))
        yield OptionList(*items)

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        try:
            options.highlighted = options.get_option_index(self._current)
        except Exception:
            pass
        height = min(16, len(self._choices) + 2)
        x, y = self._anchor.x - 1, self._anchor.bottom
        if y + height > self.size.height:
            y = max(0, self._anchor.y - height)
        options.styles.offset = (max(0, x), y)
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)

    def on_click(self, event: events.Click) -> None:
        if event.widget is self:
            self.dismiss(None)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------- custom dates and the cycle day

class DateRange(Horizontal):
    """`从 [YYYY-MM-DD] 到 [YYYY-MM-DD]` under a sentence whose range is 自定义日期."""

    DEFAULT_CSS = f"""
    DateRange {{ height: 1; padding: 0 1; display: none; color: {MUTED}; }}
    DateRange.shown {{ display: block; }}
    DateRange > Static {{ width: auto; color: {MUTED}; }}
    DateRange > Input {{
        width: 14; height: 1; border: none; padding: 0 1; background: {SURFACE}; color: {BRIGHT};
    }}
    DateRange > Input:focus {{ border: none; background: #2d3038; background-tint: {BG} 0%; }}
    DateRange > .message {{ color: {MUTED}; }}
    DateRange > .message.error {{ color: {WARN}; }}
    """

    class Submitted(Message):
        def __init__(self, section: str, start: str, end: str) -> None:
            super().__init__()
            self.section, self.start, self.end = section, start, end

    def __init__(self, section: str, start: str = "", end: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.section, self._start, self._end = section, start, end

    def compose(self) -> ComposeResult:
        yield Static("从 ")
        yield Input(self._start, placeholder="YYYY-MM-DD", max_length=10, classes="start")
        yield Static(" 到 ")
        yield Input(self._end, placeholder="YYYY-MM-DD", max_length=10, classes="end")
        yield Static("", classes="message")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        start = self.query_one(".start", Input).value.strip()
        end = self.query_one(".end", Input).value.strip()
        self.post_message(self.Submitted(self.section, start, end))

    def focus_start(self) -> None:
        self.call_after_refresh(self.query_one(".start", Input).focus)

    def set_message(self, text: str, error: bool = False) -> None:
        """A muted prompt, or with `error` a warning, after the inputs; "" clears it."""
        message = self.query_one(".message", Static)
        message.update(("  " + text) if text else "")
        message.set_class(error, "error")


class CycleDayScreen(ModalScreen):
    """Ask for the day of the month a billing cycle starts on (1-28, 00:00 UTC)."""

    BINDINGS = [Binding("escape", "dismiss_none", "取消", show=False)]
    DEFAULT_CSS = f"""
    CycleDayScreen {{ align: center middle; }}
    CycleDayScreen > Vertical {{
        width: 50; height: auto; padding: 1 2; background: {SURFACE}; border: round #3a3d45;
    }}
    CycleDayScreen Static {{ color: {MUTED}; }}
    CycleDayScreen .title {{ color: {BRIGHT}; }}
    CycleDayScreen Input, CycleDayScreen Input:focus {{
        margin: 1 0 0 0; border: none; height: 1; padding: 0 1; background: #2d3038; background-tint: {BG} 0%;
    }}
    CycleDayScreen .error {{ color: {WARN}; height: 1; }}
    """

    def __init__(self, current: int) -> None:
        super().__init__(classes="-dialog-dim")
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("计费周期开始日", classes="title")
            yield Static("每月这一天的 00:00 UTC 开始新周期（1–28）")
            yield Input(str(self._current), restrict=r"[0-9]*", max_length=2)
            yield Static("回车保存 · Esc 取消", classes="error")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value.strip()
        if text.isdigit() and 1 <= int(text) <= 28:
            self.dismiss(int(text))
        else:
            self.query_one(".error", Static).update("请输入 1–28 的整数")

    def action_dismiss_none(self) -> None:
        self.dismiss(None)
