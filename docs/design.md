# billing-usage TUI 设计

日期：2026-10-22 · 分支：`billing-usage-tui` · 插件：`plugins/billing-usage`

## 为什么做

现在查用量要先调用 `/billing-usage:report`：模型读 skill，跑 `billing_usage.py`，再把表格抄进回复。计算是确定的，模型只是转手，每次查看都多花一轮调用（本机 transcript 里 `billing-usage:report` 已经记了 7 次请求），而且输出是一张宽表，不直观。

要的是：一个常驻在终端里的 TUI，不经过模型，定时刷新，覆盖 devices 文件里的所有设备，查看逻辑和设计照着 [token-meter](https://github.com/FrankCyh/token-meter) 的 VS Code 侧边栏来做。

## 目标

- 终端 TUI，单独开一个终端标签或分屏常驻，每 60 秒自动刷新，零模型调用。
- 覆盖 devices 文件（`${XDG_CONFIG_HOME:-~/.config}/billing-usage/devices.json`）里的所有设备，沿用现在的取数方式：`local` 直接读，`ssh` 把脚本经 stdin 送过去执行、读回 JSON，远端不安装、不写任何东西。
- 照搬 token-meter 的查看逻辑：两块视图，每块顶部一句"句子"，句子里的词可切换；上面时间线，下面占比；模型、项目颜色固定；选择会被记住。
- 除 token-meter 的分组外，新增"按设备"和"按 skill"。
- Claude Code 状态栏常驻显示"今日 / 本周期"花费。

## 非目标

- 不做 token-meter 的 OpenTelemetry 推送 + 服务器架构，也不做 Web 或 VS Code 界面。
- 不显示订阅实际扣费；`cost` 仍是 API 标价折算，和现在的 report 一致。
- 不统计 claude.ai、不在 devices 文件里的设备、被 `cleanupPeriodDays` 删掉的会话（和 report 相同）。
- 不做预算告警、不做 token-meter 的 Intelligence Index 列。
- 不删、不改 `/billing-usage:report` 和 `/billing-usage:setup` 的行为。

## 反目标（技术上能跑但算失败）

- TUI 的本周期总额和 `/billing-usage:report <同一开始日>` 的 TOTAL 对不上（同一组设备、同一时刻，差一分钱也算）。
- 刷新或查看任何数字需要调用模型。
- 在远端设备上安装或写文件。
- 一台设备离线就让整个界面报错或清空；离线设备必须单独标出来，其他设备照常显示。
- 状态栏因此变慢，或在状态栏里连 ssh。
- 画面拥挤、字符错位，看起来比 token-meter 粗糙。

## 现状（事实）

- 计算全在 `plugins/billing-usage/scripts/billing_usage.py`（纯标准库，288 行）：扫 `<config_dir>/projects/**/*.jsonl`，只取 `type == "assistant"` 且有 `usage`、模型不是 `<synthetic>` 的行；按 `(message.id, requestId)`（无 id 时用 `uuid`）去重，保留 `output_tokens` 最大的那行、取最早的时间戳；按 `PRICES` 折算花费（含 1h cache write 和 Haiku 5.5 长 prompt 档）；`--json` 模式供 ssh 远端调用，输出按 UTC 日汇总。
- 本机 81 MB / 41 个 jsonl，全量扫描 0.44 秒。所以每次刷新全量重扫就够，不需要增量索引。
- transcript 每条 assistant 行都带 `cwd`、`isSidechain`，几乎都带 `effort`（`xhigh`/`high`/`medium`），在 skill 里的调用带 `attributionSkill`（例如 `investigate`、`career-ops`、`tavily:tavily-research`）和部分 `attributionPlugin`。
- 用户的状态栏是 `~/.claude/scripts/statusline.sh`（两行，bash + jq），已经读取 `rate_limits.seven_day.resets_at` 和 `five_hour`。注释里写明：在 Ghostty + JetBrainsMono Nerd Font 下，只有 `█`/`░` 这类方块字符能稳定占一格，`▰▱`、`⬢⬡` 会重叠。
- 当前 devices 文件有 4 台：`macbook-pro`（local）、`mac-mini`、`gx10-090e`、`gx10-c757`（ssh，走 Tailscale）。
- 本机有 `uv`（0.11）和 Python 3.13，没装 Textual。

## 用户定下的

- 终端为主，TUI。
- 多设备，沿用现有 devices 文件和 ssh 拉取。
- 查看逻辑和设计尽量做到 token-meter 那样。
- 认可了下面这张草图和其中的取舍（饼图改占比条、时间范围、状态栏）。

```
 Billing Usage   今日 $38.6 · 本周期 $412 · 4/4 台在线 · 1 分钟前刷新      r 刷新  q 退出
──────────────────────────────────────────────────────────────────────────────────────
 花费 · 所有设备 · 按模型 · 每 4 小时 · 最近 7 天                                $286
 $40┤                    ▆                          █
    │          ▃         █  ▂            ▅          █  ▃
    │    ▂     █  ▂      █  █     ▃      █    ▄     █  █
    └──────────────────────────────────────────────────────
     10/16   10/17   10/18   10/19   10/20   10/21   10/22
 ■ opus-5-5 xhigh 71%   ■ opus-5-5 high 22%   ■ sonnet-5-5 5%   ■ 其他 2%
──────────────────────────────────────────────────────────────────────────────────────
 花费 · 所有设备 · 按项目 · 本计费周期                                           $412
 项目             占比                       花费     请求     tokens
 nasa718          ███████████████░░░  38%    $157    1,204    41.2M
 career-ops       ████████░░░░░░░░░░  21%     $87      870    22.0M
```

## 设计

### 1. 数据层（改 `billing_usage.py`，保持纯标准库）

- 新增 `--rows` 模式：输出一行 JSON，内容是去重后的**逐请求**记录，供 TUI 本地调用和 ssh 远端调用共用。每条记录至少有：
  - `t`：最早时间戳，epoch 毫秒
  - `model`
  - `effort`：没有就是 `"none"`
  - `project`
  - `skill`：`attributionSkill`，没有就是 `"none"`
  - `input`、`output`、`cache_write`（其中 1h 部分单列）、`cache_read`
  - `cost`：没有价格就是 `null`
  - `premium`：`speed == "fast"` 或 `inference_geo == "us"`
- 去重、过滤、计价必须和现有 `collect` / `cost_of` 走**同一份代码**（重构成共用函数），不能复制一份。`--start` / `--end` 限定时间窗口，语义和现在一样。
- 现有默认输出和 `--json` 保持不变，report skill 不受影响。远端每次都是接收本机送过去的脚本，所以两端版本永远一致，不用考虑兼容。
- `project` = `cwd` 的最后一段路径，统一小写（照 token-meter 的做法；子目录会被当成另一个项目，这个误差可以接受）。

### 2. TUI（新文件 `plugins/billing-usage/scripts/billing_usage_tui.py`）

- **运行方式**：Python + [Textual](https://textual.textualize.io/)，用 PEP 723 内联依赖，靠 `uv run --script` 启动，不往系统 Python 里装任何东西。TUI 以模块方式导入 `billing_usage.py` 的函数，自己不重写任何计算逻辑。
- **取数**：启动时先显示上次的缓存（立即出画面），然后所有设备并行刷新；之后每 60 秒刷新一次，`r` 手动刷新。ssh 设备失败时保留它上次成功的数据，标成"离线 · 最后成功于 HH:MM"，并显示现有 `HINTS` 给出的修复提示。头部显示"N/M 台在线"；离线设备的旧数据计入数字时要明确标注。
- **缓存**：`${XDG_CACHE_HOME:-~/.cache}/billing-usage/`
  - `rows-<device>.json`：每台设备上次成功的逐请求数据
  - `summary.json`：给状态栏读，内容是 `today_cost`、`cycle_cost`、`devices_ok`、`devices_total`、`updated_at`
- **时间**：时间线、"今天"、按天分桶都用本地时区（同 token-meter）。"本计费周期"用 UTC 日期边界，和 report 保持一致，保证总额能对上。

#### 视图（照 token-meter 两块）

上面一块是时间线，下面一块是占比表。每块顶部一句话，句子里的每个词都能切换（鼠标点击，或 Tab 聚焦后回车选择），整句右侧显示该块的总值。

| 句子里的词 | 选项 |
| --- | --- |
| 指标 | 花费 / tokens（input+output+cache read+cache write）/ 请求数 |
| 设备 | 所有设备 / 任一台 |
| 分组 | 模型×思考强度 / 项目 / 设备 / skill |
| 粒度（仅时间线） | 1 分钟 / 5 分钟 / 15 分钟 / 30 分钟 / 1 小时 / 4 小时 / 1 天 |
| 范围 | 今天 / 本计费周期 / 自每周重置以来 / 最近 7 天 / 最近 30 天 / 全部 / 自定义日期 |

- **时间线**：每个桶一根堆叠柱，每个分组是一段颜色。横轴从第一个有用量的桶开始。图例换行显示在图下方，每项附占比。
  - 切换范围时，粒度跟着变成该范围的默认值（见下面"时间线的交互细节"）。
  - `+`/`-`/滚轮缩放，`←`/`→` 平移；选中柱子、图例项，以及只看某一段的规则，也都在下面。
- **占比表**（代替饼图）：每个分组一行，列为名称、占比条、%、花费、请求数、tokens。按模型分组时多两列：input/output 单价、cache read/write 单价。行数多时可滚动。占比低于 0.1% 的不单列，但计入总值。
- **颜色**（色值取自 token-meter `extension/media/common.js`，该仓库需要登录 GitHub 才能访问，下面已经全部抄录）：
  - 6 条色带，每条 5 个深浅，依次对应 effort 的 `low, medium, high, xhigh, max`，中间一档（high）就是该色位的代表色：
    ```
    0 蓝  #184f95 #256abf #3987e5 #6da7ec #9ec5f4
    1 橙  #8a2d00 #b3400a #d95926 #e68765 #f2b098
    2 绿  #046042 #037f58 #199e70 #68b693 #9dcfb6
    3 金  #6e4701 #915f06 #b57808 #cb9955 #ddbb90
    4 紫  #4e448c #675cb4 #8276d9 #9f99e3 #bebbee
    5 玫红 #8f2a51 #b03866 #d55181 #e3809f #f0abbe
    ```
  - 灰色 `#5b5e66`：用于没有 effort 的请求和"其他"。
  - 模型色位：按 `claude-opus-5-5, claude-sonnet-5-5, claude-haiku-5-5, claude-fable-5-1, claude-opus-5, claude-sonnet-5` 依次占 0–5 号色带；不在名单里的模型，按名字排序后依次占后面的色位（序号对 6 取模）；带日期后缀的模型 ID 归到它的基础模型。*（在 token-meter 的名单里加入 fable-5-1 是我的决定。）*
  - 项目、设备、skill 的颜色按名字哈希：`h = 7; 对每个字符 h = (h*31 + 字符编码) mod 2^32`，取第 `h mod 6` 号色带的最浅一档。这和 token-meter 的 `projectColor` 一致，颜色不随排名变化。
  - 用 truecolor（Ghostty 支持）。
- **"其他"的规则**：
  - 按模型分组时，每个模型×effort 都单独成段。
  - 按项目、设备或 skill 分组时，前 6 名单独成段，其余合并成一段灰色的"其他 · N 个"；如果"其他"里只有一项，就直接显示它本身。占比表照常列出每一行，归入"其他"的行用灰色显示。*（token-meter 只对饼图这样处理；时间线也这样做是我的决定，因为终端里颜色太多分不清。）*
  - 占比低于 0.1% 的分组不出现在图例和表格中，但计入总值。
- **时间线的交互细节**（同 token-meter `timeline.js`）：
  - 图上方居中显示这个范围的总值；如果当前只看某一段，就显示那一段的总值。
  - 选中或悬停一根柱子：列出该桶所有非零的段，按从大到小排，分三列（名称、值、占比），最后一行是该桶合计（合计行不写占比）。
  - 选中或悬停一个图例项：其他段变暗，并显示这一项在整个范围里的值和占比。
  - "只看这一段"是开关：对同一段再按一次就恢复；切换分组后如果这一段不存在了，自动恢复。
  - 缩放规则：最窄的可视窗口是 30 分钟。从看到整条轴到放大到最窄，粒度在对数尺度上从用户选的那一档逐步过渡到 1 分钟，每一步取 1/5/15/30 分钟、1/4 小时、1 天中最接近的一档。粒度永远不会比用户选的更粗；没有放大时就是用户选的粒度。
  - 范围对应的默认粒度：今天 30 分钟；自每周重置以来、最近 7 天 4 小时；最近 30 天、全部、本计费周期 1 天；自定义日期保持当前粒度不变。
- **字符**：只用 `█ ░ ▁▂▃▄▅▆▇` 和常规框线字符。
- **记住选择**：两块的所有选择和自定义日期保存在 `${XDG_STATE_HOME:-~/.local/state}/billing-usage/tui.json`。
- **布局**：目标尺寸 100×30 及以上；更窄或更矮时，上下两块各自压缩，不能乱掉。

#### 时间范围的定义

- 4 台设备用的是同一个 Claude 订阅账号（用户确认），所以计费周期和每周重置时间全局只有一个，不需要按账号分组。
- **本计费周期**：设置里配置每月的周期开始日 `cycle_start_day`（1–28），默认是 8（用户确认周期从每月 8 号开始），本周期 = 不晚于现在的最近一个该日期（UTC 00:00）。可以在 TUI 里改，保存到上面的 state 文件。
- **自每周重置以来**：取 Claude Code 给状态栏的 `rate_limits.seven_day.resets_at`，减去 7 天。状态栏脚本把它写进 `${XDG_CACHE_HOME:-~/.cache}/billing-usage/limits.json`，TUI 读取。还没有这个值时，这个选项显示为灰色并说明原因。
- **今天**：本地午夜到现在。
- **最近 N 天**：从现在往回滚动 N 天。

### 3. 状态栏（改 `~/.claude/scripts/statusline.sh`）

- 第一行，在会话花费后加一段：`今日 $38.6 · 周期 $412`。只用 jq 读 `summary.json`，不跑 Python、不连 ssh。
- `updated_at` 超过 10 分钟就把这段调暗并标"旧"；没有这个文件就不显示这段。
- 每次运行时，如果 stdin 里有 `rate_limits.seven_day.resets_at`，就写入 `limits.json`；值没变就不写。
- 脚本的其余行为保持不变。

### 4. 文档

更新 `README.md` 的 billing-usage 一节：怎么启动 TUI（例如 `uv run --script <插件目录>/scripts/billing_usage_tui.py`，建议加个 alias），以及 TUI 和 report 的关系。

## 验收

1. 同一组设备、同一时刻，TUI"本计费周期"的花费、请求数和各类 token，与 `billing_usage.py --start <周期开始日>` 的 TOTAL 完全一致。
2. 从启动到每次刷新，全程不调用模型；离开 Claude Code 也能独立运行。
3. 拔掉一台 ssh 设备（或改错地址）：界面照常显示，该设备标为离线并给出修复提示，其他数字正常。
4. 状态栏多出的这段耗时 < 50 ms，并且没有 `summary.json` 时不报错。
5. 数据层有测试：去重、按请求拆出 effort/project/skill、各时间范围的边界（尤其是 UTC 计费周期和本地"今天"）。

## 留给实现者决定

- 时间线怎么画：自己用 Textual 绘制方块字符，还是用 `textual-plotext` 等现成组件。标准是颜色固定、堆叠正确、字符不错位。
- ssh 拉取的数据量控制：例如只拉最早需要的范围、复用 ssh 连接（ControlMaster）。约束：4 台设备一次刷新要明显少于 60 秒，并且不往远端写文件。
- 快捷键的具体分配、空数据和加载中的显示方式。
- 测试怎么组织（仓库目前没有测试）。
