# 本地字幕切分 API（Python + FastAPI）

接收**按时间排序、互不重叠的词级转写**（每个词含文字与起止毫秒），用**动态规划**寻找覆盖全部词、保持原有顺序的合法字幕切分，并支持 SRT 导出。另提供**视频删段重排**接口：给定原视频上的删除区间，丢弃被完整覆盖的词、将保留词按累计删除时长前移，再对各连续片段分别优化切分。还提供**遮挡避让与上下位置联合决策**接口：给定视频时间轴上的遮挡区间及其所在区域（`top` / `bottom`），在同一套切分规则之上联合决定每条字幕的分条与垂直位置，相邻字幕换区加 1 分，并以 WebVTT cue 行位置呈现。另提供**本地台词集锦规划**接口 `POST /highlights`：复用同一份词级转写，按词文本精确匹配目标短语，从连续原词中截取互不重叠、不超单片时长上限的片段，使每个目标短语都被某次命中完整包含，并最小化总片长（毫秒）加每片 1000 分的总分。

服务启动后监听 **8000 端口**（见下文 [一键启动](#一键启动docker-compose)）：根信息 http://localhost:8000/ 、交互文档 http://localhost:8000/docs 。

- 优化目标（总代价）：

  ```
  Σ (该条字幕时长_秒 − 目标时长_秒)²
      + 4 ×（被字幕跨过的句末标点 / 长停顿边界数量）
      + 双行排版代价（仅当传入 max_line_chars 时）
  ```

  - 两个相邻词之间，若前一个词以句末标点（`. ! ? … 。 ！ ？`，允许后面再跟引号/括号）结尾，或两词间隔 ≥ `pause_threshold_ms`，即为一个**自然边界**；
  - 自然边界落在字幕内部（而不是字幕边缘）就算“被跨过”，每条加 4 分；
  - **双行排版（可选）**：传入正整数 `max_line_chars` 后，每条字幕至多两行，换行只能发生在**词与词之间**，每行长度按既有中西文拼接规则计算（包含词间空格；换行处的空格由换行符取代，不计入行宽），且每行不得超过该行宽。单行放得下时保持单行、排版代价为 0；否则枚举所有合法断行，取

    ```
    (第一行长度 − 第二行长度)² / max_line_chars²
    ```

    的最小值，代价相同取**更靠前**的断点；不存在合法双行排版的候选字幕不可选；
  - **换区代价（仅 `/segment/place` 系列）**：相邻两条字幕若放在不同区域（一条 `top`、一条 `bottom`），每次切换加 1 分；
  - **总代价平局时，选择字幕条数更少的方案；`/segment/place` 在条数也相同时，再按位置序列字典序比较（`bottom` 优先于 `top`）。**

- 硬约束（违反则不存在合法切分，返回 `422`）：
  - 每条字幕字数 ≤ `max_chars`（沿用原有统计口径）；
  - 每条字幕时长 ∈ [`min_duration_ms`, `max_duration_ms`]（含最后一条）；
  - 传入 `max_line_chars` 时，每条字幕必须能排成不超过两行、每行不超过该行宽；
  - **人工编辑边界（可选，两个字段可同时使用）**：
    - `required_break_after`：词下标 `k` 的数组，**必须**在第 `k` 个词与第 `k+1` 个词之间断开（即该处必须是某条字幕的边缘）；
    - `keep_together`：词下标闭区间 `[lo, hi]` 的数组，区间**内部不得断开**（词 `lo..hi` 必须落在同一条字幕里），但区间左右外缘仍可断开；
  - **遮挡避让（可选，仅 `/segment/place` 系列）**：每条字幕必须在 `top` / `bottom` 中拥有一个对其全程无正长度遮挡交集的区域（见下文接口说明）；
  - 全部词必须且只能出现一次，顺序不变。

> 下标均以本次请求 `words` 的**零基**顺序计。`required_break_after` 只能取 `0 .. 词数-2`（最后一个词之后不存在边界）；每个 `keep_together` 区间至少含两个词（`lo < hi`）；区间两两不得重叠（端点相接允许，如 `[0,1]` 与 `[2,3]`）；必断下标不得等于某区间的 `lo`、也不得落在区间内部（落在右外缘 `hi` 允许——此时整个区间作为该字幕的结尾）。下标非法或两类约束相互冲突均返回 `422`。
>
> 切分器在上述全部硬约束（时长、字数、双行行宽、人工边界）下**全局**寻找最低代价方案；若人工边界与其他硬约束矛盾导致没有合法切分，同样返回 `422`。两个端点 `/segment` 与 `/segment/srt` 共用同一次切分结果。
>
> 不传这两个字段（或传空数组）时，切分逻辑、代价与输出格式与旧版完全一致；不传 `max_line_chars` 时始终单行。

---

## 一键启动（Docker Compose）

前置条件：已安装 Docker（含 Compose v2）。

```bash
docker compose up --build
```

启动后访问：

| 用途 | 地址 |
| --- | --- |
| 服务根信息 | http://localhost:8000/ |
| 健康检查 | http://localhost:8000/health |
| Swagger 交互文档 | http://localhost:8000/docs |
| OpenAPI 描述 | http://localhost:8000/openapi.json |

端口固定为 **8000**，在 `docker-compose.yml` 的 `ports` 中映射；如需改端口，例如改成 9000：

```yaml
ports:
  - "9000:8000"
```

后台运行：`docker compose up --build -d`；停止：`docker compose down`。

---

## 接口

### `POST /segment` —— JSON 切分

请求体：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `words` | 数组 | 必填（至少 1 个） | 词级转写，按时间排序且互不重叠 |
| `words[].text` | string | 必填 | 词文本，非空 |
| `words[].start_ms` | int | 必填 | 起始毫秒，≥ 0 |
| `words[].end_ms` | int | 必填 | 结束毫秒，须 > `start_ms` |
| `max_chars` | int | `42` | 单条字幕字数上限，≥ 1 |
| `min_duration_ms` | int | `1000` | 单条字幕最短时长（毫秒） |
| `max_duration_ms` | int | `7000` | 单条字幕最长时长（毫秒） |
| `target_duration_ms` | int | `3000` | 目标时长（毫秒），代价按秒计算 |
| `pause_threshold_ms` | int | `500` | 词间间隔达到该值即视为长停顿边界 |
| `max_line_chars` | int | 不传 | 可选正整数；双行排版行宽（≥ 1）。传入后每条字幕至多两行、仅在词间断行、每行（含词间空格）不超过该行宽；不传则保持单行旧行为 |
| `required_break_after` | int 数组 | 不传 | 可选；必须在指定词下标之后断开。下标零基，取值 `0 .. 词数-2`，重复值自动去重 |
| `keep_together` | `[lo, hi]` 数组 | 不传 | 可选；词下标闭区间，内部（`lo..hi-1` 之间的边界）不得断开。每段至少含两个词（`lo < hi`），下标在 `0 .. 词数-1` 内；区间两两不重叠（允许端点相接），且不得与 `required_break_after` 冲突 |

> 约束：`max_duration_ms >= min_duration_ms`、`max_line_chars >= 1`；参数省略时使用环境变量配置的默认值（`max_line_chars`、`required_break_after`、`keep_together` 无环境变量默认值）。
>
> **人工边界示例**：9 个词中下标为 `0:Hello 1:world. 2:How 3:are 4:you 5:doing 6:today? 7:Fine 8:thanks.`。要求 `Hello` 单独成条、问候句在 `today?` 后断开，并把 `How are you` 锁在同一条：
>
> ```json
> "required_break_after": [0, 6],
> "keep_together": [[2, 4]]
> ```
>
> 可用 `examples/request_manual_edits.json` 复现：
>
> ```bash
> curl -X POST http://localhost:8000/segment \
>   -H 'Content-Type: application/json' \
>   -d @examples/request_manual_edits.json
> ```
>
> ```json
> {
>   "segment_count": 4,
>   "total_cost": 14.06,
>   "segments": [
>     { "index": 0, "text": "Hello", "start_ms": 0, "end_ms": 500,
>       "duration_ms": 500, "word_start": 0, "word_end": 0, "crossed_boundaries": 0 },
>     { "index": 1, "text": "world.", "start_ms": 500, "end_ms": 1400,
>       "duration_ms": 900, "word_start": 1, "word_end": 1, "crossed_boundaries": 0 },
>     { "index": 2, "text": "How are you doing today?", "start_ms": 2000, "end_ms": 4600,
>       "duration_ms": 2600, "word_start": 2, "word_end": 6, "crossed_boundaries": 0 },
>     { "index": 3, "text": "Fine thanks.", "start_ms": 5200, "end_ms": 6400,
>       "duration_ms": 1200, "word_start": 7, "word_end": 8, "crossed_boundaries": 0 }
>   ]
> }
> ```
>
> 注意 `required_break_after: [0]` 让 `Hello`（500 ms）单独成条，因此该请求把 `min_duration_ms` 放宽为 `0`；若仍用默认 `1000`，没有合法方案，返回 `422`。`keep_together: [[2,4]]` 只禁止词 2/3、3/4 之间的断点，词 4 之后（右外缘）照常断开。

请求示例：

```bash
curl -X POST http://localhost:8000/segment \
  -H 'Content-Type: application/json' \
  -d @examples/request.json
```

响应示例（`200`）：

```json
{
  "segment_count": 3,
  "total_cost": 5.96,
  "segments": [
    {
      "index": 0,
      "text": "Hello world.",
      "start_ms": 0,
      "end_ms": 1400,
      "duration_ms": 1400,
      "word_start": 0,
      "word_end": 1,
      "crossed_boundaries": 0
    },
    {
      "index": 1,
      "text": "How are you doing today?",
      "start_ms": 2000,
      "end_ms": 4600,
      "duration_ms": 2600,
      "word_start": 2,
      "word_end": 6,
      "crossed_boundaries": 0
    },
    {
      "index": 2,
      "text": "Fine thanks.",
      "start_ms": 5200,
      "end_ms": 6400,
      "duration_ms": 1200,
      "word_start": 7,
      "word_end": 8,
      "crossed_boundaries": 0
    }
  ]
}
```

`word_start` / `word_end` 为该字幕覆盖的词下标（含端点）；`crossed_boundaries` 是该字幕内部跨过的自然边界数（理想切分通常为 0）。

传入 `max_line_chars` 后，响应中的 `text` 用换行符 `\n` 分隔两行，单行字幕不含换行符。可用 `examples/request_two_lines.json` 复现（对 4 个等长拉丁词传入 `"max_line_chars": 7`）：

```bash
curl -X POST http://localhost:8000/segment \
  -H 'Content-Type: application/json' \
  -d @examples/request_two_lines.json
```

```json
{
  "index": 0,
  "text": "aaa bbb\nccc ddd",
  "start_ms": 0,
  "end_ms": 1800,
  "duration_ms": 1800,
  "word_start": 0,
  "word_end": 3,
  "crossed_boundaries": 0
}
```

- 单行放得下 → 单行输出，排版代价 0；
- 否则只在词间断行，每行 ≤ `max_line_chars`，优先让两行等长（平局取靠前断点）；
- 中西文混排仍沿用原拼接规则：如 `你好 世界` 这类无空格相邻关系，断行处同样不补空格（例：`你好\n世界`）；
- 原有 `max_chars` 与时长约束继续生效，任何候选没有合法双行排版时不可选，整体无合法切分返回 `422`。

### `POST /segment/srt` —— SRT 导出

请求体与 `/segment` 完全相同，返回 `text/plain; charset=utf-8` 的 SRT 文档：

```bash
curl -X POST http://localhost:8000/segment/srt \
  -H 'Content-Type: application/json' \
  -d @examples/request.json
```

```srt
1
00:00:00,000 --> 00:00:01,400
Hello world.

2
00:00:02,000 --> 00:00:04,600
How are you doing today?

3
00:00:05,200 --> 00:00:06,400
Fine thanks.
```

保存到文件：

```bash
curl -X POST http://localhost:8000/segment/srt \
  -H 'Content-Type: application/json' \
  -d @examples/request.json -o subtitles.srt
```

传入 `max_line_chars` 时，SRT 字幕块内用真实换行输出两行：

```srt
1
00:00:00,000 --> 00:00:01,800
aaa bbb
ccc ddd
```

`required_break_after` 与 `keep_together` 对 SRT 同样生效，且与 `/segment` 使用**同一套参数、同一份切分结果**（仅输出格式不同）：

```bash
curl -X POST http://localhost:8000/segment/srt \
  -H 'Content-Type: application/json' \
  -d @examples/request_manual_edits.json
```

```srt
1
00:00:00,000 --> 00:00:00,500
Hello

2
00:00:00,500 --> 00:00:01,400
world.

3
00:00:02,000 --> 00:00:04,600
How are you doing today?

4
00:00:05,200 --> 00:00:06,400
Fine thanks.
```

### `POST /segment/cut` —— 视频删段后的字幕重排（JSON）

在 `/segment` 的基础上接收**按原视频毫秒计的删除区间**，先“剪掉”被删除的片段再重排字幕：

- **删除区间校验**（违反任一规则返回 `422`）：`deletions` 至少含 1 个区间；每个区间为 `[start_ms, end_ms]` 且 `start_ms < end_ms`、均 ≥ 0；区间按起点**时间升序**给出、两两**互不重叠**（端点相接允许，如 `[2000, 3200]` 与 `[3200, 4600]`）；
- **词的去留**：完整落在某个删除区间内的词被**丢弃**；词与删除边界相交但未被完整覆盖（被剪断、或仅被多个区间的并集覆盖）返回 `422`——删除边界不允许切穿任何一个词；与区间端点恰好相接的词不受影响；
- **时间前移**：每个保留词的起止时间减去它之前所有删除区间的累计时长，得到剪后视频的新时间轴（片头删除会让整体提前，片尾删除不影响字幕）；
- **分段切分**：删除区间两侧的词必须分属不同字幕，因此每个连续保留片段独立套用与 `/segment` 完全相同的字数、时长、双行排版规则与代价函数分别优化；`total_cost` 为各片段代价之和；
- **下标**：响应中的 `word_start` / `word_end` 仍是**原请求** `words` 的零基下标（被删除的词不再出现，下标可能跳号）；`start_ms` / `end_ms` 为前移后的新时间；
- **无解**：删除区间覆盖全部词（无词可排），或任一连续片段在现有限制下无合法切分，均返回 `422`（错误信息注明是第几个片段）。

请求体在 `/segment` 字段（`words`、`max_chars`、`min_duration_ms`、`max_duration_ms`、`target_duration_ms`、`pause_threshold_ms`、可选 `max_line_chars`）之上增加：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `deletions` | `[start_ms, end_ms]` 数组 | 必填（至少 1 个） | 原视频时间轴上的删除区间（毫秒，≥ 0），按起点升序、互不重叠、`start_ms < end_ms` |

> 该接口不接受 `required_break_after` / `keep_together`；删除区间本身即是最强的分段约束。无删除需求时请使用 `/segment`。

请求示例（删除原视频 1.5s–5.0s，覆盖 `How are you doing today?` 四个词）：

```bash
curl -X POST http://localhost:8000/segment/cut \
  -H 'Content-Type: application/json' \
  -d @examples/request_cut.json
```

响应示例（`200`）：

```json
{
  "segment_count": 2,
  "total_cost": 5.8,
  "segments": [
    {
      "index": 0,
      "text": "Hello world.",
      "start_ms": 0,
      "end_ms": 1400,
      "duration_ms": 1400,
      "word_start": 0,
      "word_end": 1,
      "crossed_boundaries": 0
    },
    {
      "index": 1,
      "text": "Fine thanks.",
      "start_ms": 1700,
      "end_ms": 2900,
      "duration_ms": 1200,
      "word_start": 7,
      "word_end": 8,
      "crossed_boundaries": 0
    }
  ]
}
```

注意第二条字幕：`Fine` / `thanks.` 原时间为 5200–6400 ms，减去此前累计删除的 3500 ms 后前移到 1700–2900 ms；`word_start` 仍为原请求下标 `7`。

### `POST /segment/cut/srt` —— 删段重排的 SRT 导出

请求体与 `/segment/cut` 完全相同，返回同一份切分结果的 SRT 文档（`text/plain; charset=utf-8`）：

```bash
curl -X POST http://localhost:8000/segment/cut/srt \
  -H 'Content-Type: application/json' \
  -d @examples/request_cut.json
```

```srt
1
00:00:00,000 --> 00:00:01,400
Hello world.

2
00:00:01,700 --> 00:00:02,900
Fine thanks.
```

### `POST /segment/place` —— 遮挡避让的分条与上下位置联合决策（JSON）

视频画面顶部或底部有时被台标、水印、弹幕条等**遮挡**。该接口在 `/segment` 的全部参数（含可选 `max_line_chars`、`required_break_after`、`keep_together`）之上，再接收按视频时间轴给出的**遮挡区间**及其区域，联合决定每条字幕如何分条、放在顶部还是底部：

- **区域可用性**：屏幕只有两个垂直区域——`top` 与 `bottom`。某条字幕的时段 `[start_ms, end_ms)` 与某个同区域遮挡区间存在**正长度交集**时，该区域对这条字幕不可用；仅在端点处相接（交集长度为 0）**不算**遮挡，例如字幕在 1400 ms 结束、遮挡从 1400 ms 开始则互不影响；
- **每条字幕都必须有一个全程可用区域**：即整条字幕的持续时间内，所选区域没有任何与之正长度相交的同区域遮挡；
- **联合优化**：分条候选沿用与 `/segment` 完全相同的字数、时长、双行排版与自然边界规则，同一次动态规划同时选出分条方案与每条字幕的位置；总代价为

  ```
  切分代价（与 /segment 相同的目标函数）
      + 1 × 相邻字幕换区次数（top↔bottom 每切换一次加 1 分）
  ```

- **平局规则（依次）**：① 字幕条数更少；② 位置序列按 `bottom < top` 做字典序比较——`bottom` 在每个位置上都优先于 `top`，由第一条出现差异的字幕决定；
- **无可行方案返回 `422`**：不存在每条字幕都能在某一全程可用区域落位的切分（例如同一时段上下都被遮挡，或遮挡与字数 / 时长 / 行宽约束矛盾）。

请求体在 `/segment` 字段之上增加：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `occlusions` | 对象数组 | 必填（至少 1 个） | 遮挡区间列表，每项含 `start_ms`、`end_ms`（毫秒，≥ 0，`start_ms < end_ms`）与 `region`（`"top"` 或 `"bottom"`）。同一区域的区间须按起点升序、两两不重叠（端点相接允许）；`top` 与 `bottom` 的区间互相独立，可任意重叠 |

> 校验说明：同一 `region` 的区间重叠或乱序返回 `422`；不同 `region` 的区间互不影响。JSON 输出在原有每条字幕字段之上增加 `position`（`"top"` / `"bottom"`）；`total_cost` 包含换区罚分。

请求示例（片头底部被遮挡，之后顶部被遮挡；最优方案为第一条顶部、其余底部，换区 1 次）：

```bash
curl -X POST http://localhost:8000/segment/place \
  -H 'Content-Type: application/json' \
  -d @examples/request_place.json
```

响应示例（`200`）：

```json
{
  "segment_count": 3,
  "total_cost": 6.96,
  "segments": [
    {
      "index": 0,
      "text": "Hello world.",
      "start_ms": 0,
      "end_ms": 1400,
      "duration_ms": 1400,
      "word_start": 0,
      "word_end": 1,
      "crossed_boundaries": 0,
      "position": "top"
    },
    {
      "index": 1,
      "text": "How are you doing today?",
      "start_ms": 2000,
      "end_ms": 4600,
      "duration_ms": 2600,
      "word_start": 2,
      "word_end": 6,
      "crossed_boundaries": 0,
      "position": "bottom"
    },
    {
      "index": 2,
      "text": "Fine thanks.",
      "start_ms": 5200,
      "end_ms": 6400,
      "duration_ms": 1200,
      "word_start": 7,
      "word_end": 8,
      "crossed_boundaries": 0,
      "position": "bottom"
    }
  ]
}
```

说明：遮挡 `[0, 1500)` bottom 使第一条字幕（0–1400）不能放底部；遮挡 `[1900, 10000)` top 使后两条不能放顶部。全部放顶部 / 全部放底部均不可行，最小代价为切分代价 5.96 + 换区 1 次 × 1 = 6.96，位置序列 `top, bottom, bottom`。没有遮挡影响时，所有字幕都按平局规则放在 **bottom**。

### `POST /segment/place/vtt` —— 带位置的 WebVTT 导出

请求体与 `/segment/place` 完全相同，返回 `text/vtt; charset=utf-8` 的 **WebVTT** 文档；每条字幕的位置用 cue 设置行的 **`line` 行位置**呈现：顶部为 `line:0`（自顶部第 0 行），底部为 `line:-1`（自底部倒数第 1 行）。时间戳采用 WebVTT 的 `HH:MM:SS.mmm`（点号分隔毫秒）：

```bash
curl -X POST http://localhost:8000/segment/place/vtt \
  -H 'Content-Type: application/json' \
  -d @examples/request_place.json
```

```vtt
WEBVTT

1
00:00:00.000 --> 00:00:01.400 line:0
Hello world.

2
00:00:02.000 --> 00:00:04.600 line:-1
How are you doing today?

3
00:00:05.200 --> 00:00:06.400 line:-1
Fine thanks.
```

保存到文件：

```bash
curl -X POST http://localhost:8000/segment/place/vtt \
  -H 'Content-Type: application/json' \
  -d @examples/request_place.json -o subtitles.vtt
```

JSON 与 VTT 两个端点共用**同一次联合决策结果**（仅输出格式不同）；双行字幕的换行同样以真实换行出现在 cue 文本中。

### `POST /highlights` —— 本地台词集锦规划

给定一份词级转写（与 `/segment` 同一套“按时间排序、互不重叠”的 `words`，**至多 100 个词**）、**1～8 个目标短语**（每个短语为**非空词文本数组**）以及正整数**单片最长毫秒数**，规划一组时间互不重叠的**片段（clip）**，从原片里剪出“每个目标短语至少出现一次”的集锦：

- **精确匹配（允许重复、重叠命中）**：短语 `p` 在词下标 `h` 处**命中**，当且仅当连续原词 `h .. h+len(p)-1` 的 `text` 与短语各项**逐字完全相等**（区分大小写，不做大小写 / 空白归一化）。同一短语可命中多次（重复出现），不同命中之间允许重叠（例如原词 `a a a` 对短语 `["a","a"]` 在 0、1 两处各命中一次）；
- **候选片段**：片段只能由一段**连续原词** `[i, j]` 构成，时码只取首词的 `start_ms` 与尾词的 `end_ms`（时长 = 尾词结束 − 首词开始，词间停顿也计入片长），且时长 **≤ `max_clip_duration_ms`**；片段可以比命中更宽（为了在一片里同时装下多个短语而向外延伸到邻近词）；
- **覆盖定义**：片段 `[i, j]` **覆盖**短语 `p`，当且仅当它**完整包含** `p` 的某次命中（命中区间 `[h, h+len(p)-1] ⊆ [i, j]`）；只与命中相交、没把命中整个包住不算覆盖。每个目标短语都必须被选出的某一片覆盖**至少一次**；
- **互不重叠**：选出的片段在时间轴上互不重叠；由于词本身按时间排序且互不重叠，这等价于各片的词下标区间互不相交。两片仅在词边界处端点相接（前一片的 `end_ms` 等于后一片的 `start_ms`）不算重叠，允许；
- **目标函数**：在满足上述全部要求的方案中最小化**总分**

  ```
  Σ 各片段时长（毫秒） + 1000 × 片段数
  ```

- **平局规则（依次）**：① 片段数更少；② 按时间排列的片段**原词起止下标二元组序列**字典序更小者（`(start0,end0),(start1,end1),…`，由第一个出现差异的片段决定，下标小者优先）；
- **无覆盖方案返回 `422`**：某个短语在转写中没有任何精确命中；或某次命中自身跨度（首词起到尾词止）已超过单片上限；或命中之间在时间上互相重叠、又无法被同一片完整包含，因而挑不出两两不重叠的片段覆盖全部目标短语。

请求体：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `words` | 对象数组 | 必填（1～100 个） | 词级转写，按时间排序且互不重叠；字段与 `/segment` 的 `words[]` 相同（`text` 非空、`start_ms ≥ 0`、`end_ms > start_ms`） |
| `phrases` | 字符串数组的数组 | 必填（1～8 项） | 目标短语；每项为至少 1 个**非空**词文本组成的数组，按词文本与连续原词精确（区分大小写）匹配；重复、重叠命中均有效。短语下标即其在数组中的零基位置 |
| `max_clip_duration_ms` | int | 必填（`> 0`） | 单片最长毫秒数；任一片段的 `end_ms − start_ms` 不得超过它 |

请求示例（9 个词的转写，目标短语为 `Hello world.` 与 `Fine thanks.`，单片上限 2000 ms；两片命中相距约 4.6 秒，无法同片覆盖）：

```bash
curl -X POST http://localhost:8000/highlights \
  -H 'Content-Type: application/json' \
  -d @examples/request_highlights.json
```

响应示例（`200`）：

```json
{
  "clip_count": 2,
  "total_score": 4600,
  "clips": [
    {
      "index": 0,
      "start_ms": 0,
      "end_ms": 1400,
      "duration_ms": 1400,
      "word_start": 0,
      "word_end": 1,
      "covered_phrases": [0]
    },
    {
      "index": 1,
      "start_ms": 5200,
      "end_ms": 6400,
      "duration_ms": 1200,
      "word_start": 7,
      "word_end": 8,
      "covered_phrases": [1]
    }
  ]
}
```

说明：

- `clips` 按**时间升序**排列；`word_start` / `word_end` 为该片首尾原词的零基下标（含端点），`start_ms` / `end_ms` 直接取自这两个词；
- `covered_phrases` 为**该片完整包含了命中**的目标短语零基下标数组（升序、去重）；一片可覆盖多个短语，同一短语也可在多片中重复出现，但每个短语只需覆盖一次；
- 总分 `4600 = (1400 + 1000) + (1200 + 1000)`：两片片长之和加每片 1000 分。固定费鼓励在不超上限时把多个短语合进同一片——例如两个短语的命中相距很近、一片装得下时，结果只会返回一片（总分 = 该片时长 + 1000）；
- 片段只会为覆盖目标而存在：不覆盖任何短语的词绝不会被选入，片段向命中两侧扩张仅当这样能合并多个短语的覆盖、从而省下另一片的 1000 分；
- 同一短语出现多次时规划器自由选择最省时的一次（片长相同则取更早、下标字典序更小的一次）。

无覆盖方案时返回 `422`，例如短语从未出现：

```json
{"detail": "no feasible highlight plan: target phrase(s) 0 have no exact consecutive-word match in the transcript (matching is exact and case-sensitive on word text)"}
```

### `GET /health`

返回 `{"status":"ok"}`（容器健康检查同样使用该端点）。

---

## 错误处理（422）

以下情况返回 HTTP `422`，响应体 `detail` 说明原因：

- 某个词自身字数超过 `max_chars`（单词无法容纳）；
- 传入 `max_line_chars` 时，某个词自身字数超过该行宽（单词无论放在哪一行都放不下）；
- 某个词自身时长超过 `max_duration_ms`；
- 词序列未按时间排序或存在重叠；
- **人工边界输入非法或相互冲突**：
  - `required_break_after` 下标不在 `0 .. 词数-2`（例如只有 1 个词时传 `[0]`，或最后一个词之后的下标）；
  - `keep_together` 区间越界、`lo >= hi`（少于两个词）、或区间两两重叠；
  - 必断下标落在区间内部，或等于区间左外缘 `lo`（词 `lo` 既要结束上一条字幕、又要与词 `lo+1` 同条，互相矛盾）；落在右外缘 `hi` 合法；
- 参数本身不合法（空词、`end_ms <= start_ms`、`max_duration_ms < min_duration_ms`、`max_line_chars < 1`、编辑字段元素不是非负整数 / 区间不是两个整数等）；
- **删除区间相关（`/segment/cut`、`/segment/cut/srt`）**：
  - `deletions` 缺失、为空数组、区间不是两个非负整数、`start_ms >= end_ms`；
  - 区间未按起点升序给出，或两两重叠（端点相接允许）；
  - 某个词与删除边界相交但未被完整覆盖（删除边界切穿了词）；
  - 删除区间覆盖了全部词，没有任何词可排；
  - 某个连续保留片段在现有字数 / 时长 / 行宽限制下无合法切分（错误信息注明片段序号）；
- **遮挡区间相关（`/segment/place`、`/segment/place/vtt`）**：
  - `occlusions` 缺失、为空数组、`start_ms` / `end_ms` 为负、`start_ms >= end_ms`、`region` 不是 `"top"` / `"bottom"`；
  - 同一 `region` 的区间未按起点升序给出，或两两重叠（端点相接允许；`top` 与 `bottom` 互相独立）；
  - 不存在让每条字幕都拥有一个全程可用区域的切分（某时段上下区域同时被遮挡，或遮挡与字数 / 时长 / 行宽 / 人工边界等硬约束矛盾）——会返回类似：

  ```json
  {"detail": "no legal segmentation covers all words under the given constraints and occlusion areas: every subtitle must stay for its whole duration within an area (top or bottom) that has no positive-length intersection with a same-area occlusion, while respecting max_chars=42, min_duration_ms=1000, max_duration_ms=7000; try relaxing min_duration_ms / the other limits or shortening the occluded spans"}
  ```
- 没有任何切分能满足全部约束（例如最后一个词短于 `min_duration_ms`，或**人工边界迫使某条字幕违反时长 / 字数 / 行宽约束**，或传入 `max_line_chars` 后不存在满足行宽的双行排版）——会返回类似：

  ```json
  {"detail": "no legal segmentation covers all words under the given constraints (max_chars=42, max_line_chars=20, min_duration_ms=1000, max_duration_ms=7000); every subtitle — including the last one — must satisfy the duration limits (and, when set, fit on at most two lines of max_line_chars) and the required_break_after / keep_together boundaries, so try relaxing min_duration_ms or the other limits"}
  ```
- **台词集锦相关（`/highlights`）**：
  - `words` 超过 100 个、为空，`phrases` 不在 1～8 项之间，某个短语为空数组、含空字符串，或 `max_clip_duration_ms ≤ 0`；
  - 词自身非法（空文本、`end_ms <= start_ms`）或词序列未按时间排序 / 存在重叠；
  - 某个目标短语在转写中没有任何逐字精确命中（区分大小写）；
  - 存在命中但其自身首尾词跨度超过单片时长上限（任何包含该命中的片段都必然超限）；
  - 多个目标短语的命中在时间上互相重叠、且无法被同一个不超上限的片段完整包含，导致不存在两两时间不重叠的片段集合能覆盖全部短语。

---

## 配置

所有切分参数都可**按请求**传入；省略时使用环境变量配置的默认值（见 `docker-compose.yml`）：

| 环境变量 | 默认值 | 对应请求字段 |
| --- | --- | --- |
| `DEFAULT_MAX_CHARS` | `42` | `max_chars` |
| `DEFAULT_MIN_DURATION_MS` | `1000` | `min_duration_ms` |
| `DEFAULT_MAX_DURATION_MS` | `7000` | `max_duration_ms` |
| `DEFAULT_TARGET_DURATION_MS` | `3000` | `target_duration_ms` |
| `DEFAULT_PAUSE_THRESHOLD_MS` | `500` | `pause_threshold_ms` |

`max_line_chars` 没有环境变量默认值：省略该请求字段即不启用双行排版，行为与旧版完全一致。`required_break_after`、`keep_together` 同样没有环境变量默认值：省略即不施加任何人工边界。`deletions` 为 `/segment/cut` 的必填字段、`occlusions` 为 `/segment/place` 的必填字段，二者也都没有环境变量默认值；相邻字幕换区罚分固定为 1 分。

不需要外部数据库或密钥，开箱即用。

---

## 本地开发（不使用 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

访问 http://localhost:8000/docs 调试。

运行测试（核心切分逻辑只依赖标准库；API 测试需要 fastapi + httpx）：

```bash
python3 -m unittest discover -s tests
# 或
pytest
```

## 项目结构

```
app/
  config.py      # 环境变量默认值与罚分常量（含换区罚分）
  models.py      # Pydantic 请求/响应模型
  segmenter.py   # 动态规划切分核心（纯 Python，无 Web 依赖）
  cut.py         # 视频删段后的重排：删词、时间前移、分片段切分
  place.py       # 遮挡避让：分条与 top/bottom 位置联合动态规划
  highlights.py  # 台词集锦：短语精确命中 + 覆盖位掩码动态规划选片
  srt.py         # SRT 时间戳与文档格式化
  vtt.py         # WebVTT 时间戳、文档格式化与 cue 行位置
  main.py        # FastAPI 入口（/segment、/segment/srt、/segment/cut、/segment/cut/srt、/segment/place、/segment/place/vtt、/highlights、/health）
tests/           # unittest 测试（核心逻辑 + 删段重排 + 遮挡避让/位置 + 台词集锦 + API 集成）
examples/        # 请求示例（基础 / 双行排版 / 人工编辑边界 / 删段重排 / 遮挡避让 / 台词集锦）
Dockerfile
docker-compose.yml
requirements.txt
```

## 算法说明

动态规划状态 `dp[i]` 为“从第 `i` 个词切分到结尾”的最小代价与字幕条数：

```
dp[n] = (0, 0)
dp[i] = 按 (总代价, 字幕条数) 字典序取最小，枚举合法的字幕结尾 j：
        ((dur_ij秒 − target秒)² + 4 × crossed(i,j) + linecost(i,j) + dp[j+1].代价,
         1 + dp[j+1].条数)
```

字幕从 `i` 扩展到 `j` 时字数与时长单调增长，超限即可提前终止；自然边界计数用前缀和做 O(1) 区间查询。

人工边界在同一个 DP 上直接剪枝，不改变目标函数：

- **必断**：再维护一个必断前缀和，候选字幕 `i..j` 内部若含任何必断边界（`i <= k < j`）即非法；一旦吞并第一个必断点，继续扩大 `j` 只会吞并更多，可直接 `break`。段首 `i` 本身位于必断边界时 DP 递归会自然落到正确的前驱；
- **保持同段**：把每个区间 `[lo, hi]` 内部的边界 `lo .. hi-1` 标记为“禁止作为段边缘”；候选段的结束边 `(j, j+1)` 落在被禁边界上时跳过（`continue`，不能 `break`，因为更远的结束位置可能合法）。

下标越界、区间少于两个词、区间重叠、必断与区间冲突等输入错误在进入 DP 前一次性校验并返回 `422`；DP 跑完若 `dp[0]` 不可达，即说明人工边界（或行宽等其他约束）导致无合法方案。人工边界只增减可行候选，枚举仍覆盖所有合法切分，因此求出的仍是满足全部边界的**全局**最低代价方案，时间复杂度保持 O(n² log n)（不传 `max_line_chars` 时 O(n²)）。

传入 `max_line_chars` 时，`linecost(i,j)` 为该候选字幕的排版代价：整段不超行宽则为 0（单行）；否则在词间枚举断点，两行各自不超行宽时计算 `(行长1 − 行长2)² / 行宽²`，取最小值（平局取靠前断点）；没有合法断点时该候选非法。行长用“词长 + 词间空格”的前缀和按二分搜索求值，合法断点范围与等长断点都只需 O(log n) 定位，因此整体时间复杂度仍为 O(n² log n)（不传该参数时为 O(n²)），空间 O(n)。

删段重排在进入 DP 之前做一次线性扫描：删除区间先校验升序、不重叠、`start < end`；随后按时间顺序遍历词，用指针推进区间——完整落入区间的词丢弃，与边界相交但未完整覆盖的词直接报错，保留词减去此前累计删除时长；凡是跨过删除区间的位置都开启新的连续片段。由于每个删除区间强制成为字幕边缘，总代价与硬约束都按字幕可加，因此各片段独立跑一次上述 DP 即等价于全局强制断点后的最优解；各片段的字幕顺序拼接、统一编号，`word_start` / `word_end` 映射回原请求下标。

遮挡避让把“位置”纳入同一个动态规划，而不是先切分再摆位置：遮挡区间先按区域分别校验、排序，可用性只需对合并后的区间数组做一次二分查找。状态 `dp[i][area]` 表示“从第 `i` 个词切到结尾、且第一条字幕放在 `area`（top / bottom）”的最优方案，枚举的候选字幕与普通切分完全一致（共用同一套候选枚举与排版代价）；对每个在该区域**全程无正长度遮挡交集**的候选 `i..j`，从 `dp[j+1][next]` 两个区域的状态转移，当后续仍有字幕且 `next ≠ area` 时加上 1 分换区罚分。区域可用性随候选跨度单调：某区域一旦被遮挡，从同一起点继续扩展的候选在该区域也不可用，两个区域都不可用时即可停止枚举。平局比较按 `(总代价, 字幕条数, 区域序列)` 字典序，其中区域以 `bottom=0 < top=1` 编码，故等价于“条数更少，再 bottom 优先”。两个起点区域都不可达即返回 `422`。时间复杂度仍为 O(n²)（每个状态保存区域序列用于平局比较，空间 O(n²)）。

台词集锦先对全部短语做朴素的连续词文本比对（至多 8 个短语、100 个词，代价可忽略），记录每个词下标 `j` 上“以 `j` 结尾”的所有命中 `(h, 短语位)`；再枚举每个起点 `i` 的合法片段 `[i, j]`：时长随 `j` 单调增长，超过 `max_clip_duration_ms` 即停止；片段的覆盖位掩码在扫到新命中尾词时增量并入（命中起点 `h ≥ i` 才算被该片段完整包含），不覆盖任何短语的片段不产生候选。动态规划状态 `dp[i][need]` 表示“只使用词 `i..n-1` 范围内的片段、覆盖 `need` 中所有短语位（允许多覆盖）”的最优方案，转移有两类：跳过词 `i`（取 `dp[i+1][need]`），或以 `i` 为首词取一个候选片段 `[i, j]`（得分 `时长 + 1000`，递归到 `dp[j+1][need & ~mask]`）。按 `(总分, 片段数, 首尾下标二元组序列)` 字典序取最小——后缀状态显式保存其下标序列，因此前缀相同时比较后缀即得到整个序列的字典序，跳过转移天然对应“第一个片段起点更靠后”的较大序列。终态 `dp[n][0] = 0`，答案为 `dp[0][(1<<m)-1]`，不可达即返回 `422`（错误信息区分“短语从未命中”与“上限 / 重叠导致无法覆盖”）；回溯时沿“跳过或取片”的选择重建片段，并从各片覆盖掩码输出短语下标。状态数 `(n+1)·2^m ≤ 101×256`，每个状态至多枚举 `n` 个候选，时间复杂度 O(n²·2^m)、空间 O(n·2^m)（另需 O(n²) 保存覆盖掩码）。
