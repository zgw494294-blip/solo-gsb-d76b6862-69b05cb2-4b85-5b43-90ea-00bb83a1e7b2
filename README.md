# 批注迁移台

本地 Web 应用：把旧版文本上的批注（`id`、`start`、`end`）迁移到新版文本；内置**批注集仲裁**模块，对同一文本上的左右两组批注做冲突裁决；提供**序列标注语料导出**模块，把文本与批注切分为 token 并分层生成 BILOU 标签，导出 CoNLL 语料；提供**嵌套批注审计**模块，按结构规则校验嵌套批注并导出稳定嵌套 JSON；提供**批注偏移制式转换**模块，在 codepoint / UTF-16 / UTF-8 字节三种偏移制式间互转并逐字符边界校验；提供**批注审阅切片**模块，把批注按窗口上限顺序装箱为审阅切片、导出 JSONL，并校验回导的审阅结果；并提供**批注标签体系迁移**模块，按声明顺序的规则链（`pattern` 用 `re.fullmatch` 匹配批注原文）把批注 label 迁移到目标标签体系。

- 后端：FastAPI；前端：原生 HTML / CSS / JavaScript（无构建步骤）
- 除制式转换模块外，其余模块偏移一律按 **Unicode 码点** 计算，区间为左闭右开 **[start, end)**
- 迁移规则：
  1. 用 `difflib.SequenceMatcher(autojunk=False)` 建立匹配块，批注**完整落在同一匹配块**内 → 直接平移（`shifted`）；
  2. 否则在新版中搜索批注原文，**唯一命中** → 迁移（`matched`）；
  3. **多处命中** → 分别取命中位置前后各 20 个码点，与旧版对应上下文计算 Levenshtein 距离之和，最小者胜出（`disambiguated`）；
  4. 距离仍并列或没有命中 → 标为**待确认**（`pending`）。
- 页面并排高亮原位置与候选，可人工选择候选（`manual`）或标记废弃（`abandoned`）；
  **存在未处理的待确认项时禁止导出**；导出保留 `id`，并写入新偏移与状态。

## 批注集仲裁（/arbitrate）

输入**同一份文本**与左右两组批注（每条含 `id`、`start`、`end`、`label`，`id` 在各自集合内唯一）：

- 左右批注区间**交集长度大于 0** 即在二者间连边，构成二分图，每个**连通分量**即一个仲裁组；
- 组内恰为**一左一右**且**范围、label 完全相同** → **自动通过**（`auto`）；
- 其余分量（一对一冲突、一对多、多对多、**单侧分量**）全部进入仲裁（`pending`），
  按**整组**裁决：**整组采用左侧** / **整组采用右侧** / **全部舍弃**——不做逐条贪心拆分；
- 页面把两侧范围**叠加**显示在同一文本上（仅左侧 / 仅右侧 / 两侧重叠三色区分）；
- **存在未处理的仲裁组时禁止导出**；导出时后端会**重新计算分组并校验决策合法性**；
- 导出的 `annotations` 按 `start`、`end`、`label`、来源 `id` 稳定排序并**重新编号**（`ann-1`、`ann-2`…），
  附 `source`（`left` / `right` / `both`）与 `source_id`；`decisions` 逐组保留
  `left_ids`、`right_ids`、`decision`（自动通过组记为 `auto`）。

## 序列标注语料导出（/sequence）

输入文本与一组批注（每条含 `id`、`start`、`end`、`label`，`id` 唯一）：

- **Token 切分**：连续汉字为一个 token，连续 ASCII 字母数字为一个 token，
  其余单个非空白字符自成一个 token；空白字符不产出 token；
- **批注边界**会切开其落在的 token，使每条批注恰好覆盖若干完整 token；
  只覆盖空白（不含任何 token）的批注直接报错（422）；
- **重叠批注不能同层**：按 `start`、`end`、`label`、`id` 稳定排序后贪心分层——
  在末条批注 `end ≤` 当前 `start` 的层中按（结束位置、层号）选最小者，
  无可用层才新建；层数即区间最大深度，必为最少；
- 每层独立生成 **BILOU** 标签：未覆盖 token 为 `O`，单 token 批注为 `U-label`，
  多 token 批注首 / 中 / 末分别为 `B-` / `I-` / `L-label`；
- 页面预览每个 token、码点范围 `[start, end)` 及各层标签；
  「下载 CoNLL」导出 **UTF-8** 编码文件，每行 `token start end 各层标签`（空格分隔）；
- 非法区间、空 `label`、重复 `id` 均返回 **422**。

## 嵌套批注审计（/nesting）

输入文本、一组批注（每条含 `id`、`start`、`end`、`label`，`id` 唯一）与结构规则
（`root_labels` 为允许的根 label 列表，`children` 把父 label 映射到允许的**直接**子 label 列表）：

- **区间关系**（端点相接不算重叠，只有**严格包含**才形成父子）：
  - 同范围 → 记 `duplicate_span` 错误；
  - 部分交叉 → 记 `crossing` 错误；
  - 严格包含 → 父子嵌套；`end == start` 的相接批注互不影响；
- **建树**：区间关系全部合法时，按 `(start, -end, label, str(id))` 排序，用栈确定
  **最窄直接父级**，得到嵌套树；存在 `duplicate_span` / `crossing` 时无法建树（`tree` 为 `null`）；
- **label 规则**：根批注的 label 须在 `root_labels` 中，非根批注的 label 须在其
  **直接父级**的 `children` 中，违反分别记 `root_label` / `child_label` 错误（不影响建树）；
- 页面展示全部错误与合法嵌套树；**存在错误时禁止导出**；导出时后端**重新计算**，
  返回保留原 `id`、`label`、`start`、`end` 的**稳定嵌套 JSON**（各层 children 按上述排序键稳定有序）；
- 非法区间、空 `label`、重复 `id` 均返回 **422**；`root_labels` 或任一 `children`
  列表含重复 label 同样返回 **422**。

## 批注偏移制式转换（/units）

输入文本、一组批注 JSON（至少含唯一 `id`、`start`、`end`，可附 `label` 等任意其余字段）
与 `source_unit`、`target_unit`，三种制式均为左闭右开 **[start, end)**：

- **codepoint**：Unicode 码点偏移（即 Python `str` 索引）；
- **utf16**：UTF-16 代码单元偏移——BMP 字符占 1，辅助平面字符（如 🎉 U+1F389）
  的**代理对占 2**；
- **utf8_byte**：UTF-8 字节偏移——每码点占 1~4 字节（汉字 3、U+0301 组合符 2、🎉 4）。

核心规则：

- 后端对文本做**一次遍历**（逐码点）同时建立三套字符边界映射（每个码点边界在三种
  制式下的偏移 + 各制式文本总长），后续校验与换算都只查映射；
- 源偏移必须落在 `source_unit` 的**字符边界**：codepoint 的整数位置天然是码点边界；
  utf16 禁止落入**代理对内部**；utf8_byte 禁止落入 **UTF-8 多字节序列内部**；
  负数、超过该制式总长或 `start >= end` 同样非法；
- **错误逐项列出**，每条含批注 `id`、字段（`start` / `end`，区间整体非法时为
  `interval`）与值（`out_of_range` / `not_boundary` / `bad_interval`），不做整体拒绝；
- 页面预览每条批注的**原文片段**（按码点切出）与**三种制式坐标**，非法批注标红、
  坐标留空；**存在非法批注时禁止导出**；
- 导出时后端**重新遍历文本、重新建立映射并重算**（不信任前端坐标），仅把 `start`、
  `end` 转为目标制式，**保持输入顺序与其余字段不变**；重复 `id` 返回 **422**。

### 往返示例（汉字 + 组合字符 + 辅助平面字符）

文本 `汉字é🎉A`（`e` 后紧跟 U+0301 **组合尖音符**，二者是分解序列、共两个码点），共 **6 个码点**：

| 码点边界 i | 字符 | codepoint | utf16 | utf8_byte |
| --- | --- | --- | --- | --- |
| 0 | （起点） | 0 | 0 | 0 |
| 1 | 汉 | 1 | 1 | 3 |
| 2 | 字 | 2 | 2 | 6 |
| 3 | e | 3 | 3 | 7 |
| 4 | ́（U+0301） | 4 | 4 | 9 |
| 5 | 🎉（代理对） | 5 | 6 | 13 |
| 6 | A | 6 | 7 | 14 |

总长：codepoint 6 / utf16 7 / utf8_byte 14。以 utf16 为源、utf8_byte 为目标：

- `u1 [0, 2)` = “汉字” → `[0, 6)`（两个汉字各 3 字节）；
- `u2 [2, 4)` = “é”（e + 组合音符，2 码点）→ `[6, 9)`（1 + 2 字节）；
- `u3 [4, 6)` = “🎉”（整个代理对）→ `[9, 13)`（4 字节）；
- `u4 start=5` → 落入 🎉 代理对内部（合法边界只有 4、6）→ `not_boundary`；
- `u5 end=9` → 超过 utf16 总长 7 → `out_of_range`。

**往返**：`codepoint [0,2) → utf16 [0,2) → utf8_byte [0,6) → codepoint [0,2)`，
任意制式间来回转换都能精确还原（边界映射只含字符边界，不会产生歧义）。

## 批注审阅切片（/review）

输入文本、一组批注（每条含**唯一字符串** `id`、`start`、`end`、`label`）与
`max_chars`、`context_chars`（单位均为码点）：

- **顺序装箱**：批注按 `(start, end, label, id)` 排序后依次考察——加入当前组后的
  跨度（组内最左 `start` 到最右 `end`）不超过 `max_chars` 就并入当前组，否则当前组
  封口、以该批注另起新组；**单条批注长度超过 `max_chars` 时报错**（422）；
- **切片窗口**：先包住组内全部批注 `[s, e)`，再把 `max_chars` 剩余的容量均分到
  两侧——每侧不超过 `context_chars`，奇数余量多给右侧；一侧触及文本边界用不完的
  余量转给另一侧（每侧仍不超过 `context_chars`）；
- **导出 JSONL**：每行一个切片，含稳定 `fragment_id`（`frag-1`、`frag-2`…）、
  片段 `text`、全局范围 `start` / `end`、片段 UTF-8 编码的 SHA-256 `sha256` 与
  **局部坐标**（窗口相对）批注；导出时后端重新装箱，不信任前端结果；
- **导入审阅结果**：后端用同一输入**重新切片**，逐条校验 `fragment_id`、切片摘要与
  批注归属（`id` 必须属于该切片），并要求**每个 id 恰好提交一次**；每条批注可
  `keep`（保留原范围）、`drop`（舍弃）或 `correct`（提交窗口内局部坐标的修正范围，
  须满足 `0 ≤ start < end ≤ 窗口长度`），校验全部通过后换回全局坐标，
  **任一错误均拒绝整批**（422 并列出全部错误）；
- 页面预览每个切片的窗口、高亮批注与局部 / 全局坐标，可一键生成全 `keep`
  审阅模板；非法区间、空 `label`、重复或非字符串 `id`、`max_chars < 1`、
  `context_chars < 0` 均返回 **422**。

## 批注标签体系迁移（/relabel）

输入文本、一组批注（每条含唯一 `id`、`start`、`end`、`label`，可附其余字段）与规则 JSON
（`target_labels` 为目标标签体系，`rules` 为迁移规则列表，每条含 `from`、`to` 与可选
`pattern`）：

- **逐条求值迁移链**：当前 label 在 `target_labels` 中 → 迁移结束；否则在当前 label
  的规则组（`from` == 当前 label）内按**声明顺序**取首个 `pattern` 命中的规则——
  `pattern` 用 Python `re.fullmatch` 匹配**批注原文**（文本的 `[start, end)` 切片，
  须整个命中）；无 `pattern` 的**回退规则**必中。把 label 改写为 `to` 后**继续求值**，
  因此可以串成多步链（如 `上古-流程 → 旧-流程 → 流程`）；
- **回退规则约束**：同一 `from` 下无 `pattern` 的回退规则**至多一条**，且必须排在该
  `from` 规则组的**最后**；
- **逐项错误**：迁移链**重复经过同一 label** 记 `cycle`（循环）；当前 label 不在
  `target_labels` 且没有可用规则记 `no_rule`。错误逐条列出，不影响其他批注求值；
- 页面展示**原文**（批注覆盖高亮）、每条批注的**迁移链**与命中规则、以及全部逐项错误；
  **存在错误时禁止导出**；
- 导出时后端**重新求值迁移链**，保持输入顺序与 `id`、范围和其余字段不变，**只替换
  label**；
- 非法区间、空 `label`、重复 `id` 返回 **422**；空 / 重复目标标签、空 `from` / `to`、
  非法正则、回退规则冲突（同一 `from` 多条或未排最后）同样返回 **422**。

## 可分享文本遮盖导出（/api/redact/export）

输入文本、一组批注（每条含**唯一** `id`、`start`、`end`、`label`）与需遮盖的标签
列表 `redact_labels`，区间沿用 Unicode 码点 `[start, end)`：

- **合并遮盖区**：`label` 命中遮盖标签列表的批注其区间只要**重叠或相接**（前一个的
  `end` 等于后一个的 `start` 也算）就合并；每个合并区在新文本中整体替换为**一个**
  固定标记 `[遮盖]`，**命中批注自身不导出**；
- **其余批注拆片段**：非命中批注跨遮盖区时切成多个可见片段，完全被遮盖的舍弃；
  片段按**原批注顺序、同批注内按原文位置**排列，保留原 `label` 并带 `source_id`
  （原批注 `id`）与**从 1 开始**的片段序号 `fragment`；
- **新码点范围**：HTTP 结果同时返回新文本与片段在新文本中的 `start` / `end`，
  范围内文字必须是原文的可见部分、**不含** `[遮盖]` 标记；
- **无命中**（没有任何批注的 `label` 在遮盖标签列表中）时新文本与批注范围完全不变
  （每条批注各导出一个 `fragment: 1` 的完整片段，`redacted_count` 为 0）；
- 非法区间、空 `label`、重复 `id`、`redact_labels` 不是字符串数组或含空标签
  均返回 **422**，**不返回部分结果**；导出时后端重新合并遮盖区并重算坐标。

沿用同一套容器启动方式（见下方「快速开始」），无需额外服务，直接 POST 接口即可。

## 可领取的审阅工作副本（/api/copies）

为现有审阅流程提供**可领取的工作副本**（纯 HTTP，无前端页面）：创建副本时
提交**固定文本**与一组批注（每条含**唯一字符串** `id`、`start`、`end`、**非空**
`label`，码点区间左闭右开 **[start, end)**），副本初始**无人持有**，持久化到
随容器启动初始化的 **SQLite**（Compose 挂卷，重启后副本保留）。

- **连通组**：两条批注区间**正长度重叠**（`a.start < b.end` 且 `b.start < a.end`）
  即在二者间连边，取**传递连通组**；**相接**（`end == start`）**不连边**。
- **领取 / 释放**：按批注 `id` 与**非空审阅者标识** `reviewer` 操作其所在**整组**
  ——组内全部批注一并领取 / 释放；组无人持有方可领取（本人重复领取幂等，不产生
  新版本），**领取他人持有的组返回 409**；**释放仅限持有者本人**（无人持有的组
  释放返回 409）。
- **修改**：仅持有者可 **PATCH** 修改**单条**批注的范围与 `label`，**不增删批注**。
  修改后在新几何上**重算分组**：
  - 新并入的**未领取**批注归该持有者（整组领取语义的延续）；
  - 新组**触及其他持有者**的批注 → 返回 **409**，**范围与持有权均不变**；
  - 原组被**拆开**时，各碎片**保留原持有者**；
  - 仅相接（不连边）不视为触及，不冲突。
- **失败原子性**：非法输入（非字符串 / 空 `id`、空 `label`、`0 ≤ start < end ≤ 文本长度`
  不满足、重复 `id`、空 `reviewer` 等）返回 **422**；任何失败（422 / 409 / 404）
  **都不改变副本**。
- **并发**：所有变更请求在服务端串行化，等价于**逐次完整执行**，不会丢失其他
  批注的修改；每次成功变更 `revision` 加 1。
- **读取**：`GET` 返回**同一提交状态**下的固定文本、全部批注（含 `holder`，未持有
  为 `null`）、按稳定顺序排列的连通组（每组附 `holder`）与 `revision`。读取到的
  批注可**直接用于原审阅接口**（如 `/api/review/preview`）。

SQLite 数据库在容器内位于 `/data/copies.db`（`docker-compose.yml` 的命名卷
`copies-data`，随容器启动自动建表，重启保留副本）；可用环境变量 `COPY_DB_PATH`
覆盖路径，本地开发默认回退到仓库根目录 `data/copies.db`（已在 `.gitignore`）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/copies` | 入参 `{text, annotations:[{id,start,end,label}]}`（`id` 为唯一字符串），创建副本，返回 **201** 与初始快照（无人持有，`revision=1`）；非法输入 **422** |
| GET | `/api/copies/{id}` | 返回 `{id, text, revision, annotations:[{id,start,end,label,holder}], groups:[{annotation_ids,holder}]}`；未知副本 **404** |
| POST | `/api/copies/{id}/annotations/{ann_id}/claim` | 入参 `{reviewer}`，按批注 id **整组领取**，返回最新快照；他人持有 **409**，`reviewer` 非法 **422**，批注 / 副本不存在 **404** |
| POST | `/api/copies/{id}/annotations/{ann_id}/release` | 入参 `{reviewer}`，**持有者整组释放**；无人持有 / 他人持有 **409**，`reviewer` 非法 **422** |
| PATCH | `/api/copies/{id}/annotations/{ann_id}` | 入参 `{reviewer, start, end, label}`，仅持有者改单条范围与 label，随后重算分组；触及他人 **409**（范围与持有权均不变），未持有 / 非法区间 / 空 label **409 / 422** |

```bash
# 创建工作副本：a 与 b 正长度重叠同组；b 与 c 相接不连边；d 独立
CID=$(curl -s http://localhost:8000/api/copies \
  -H 'Content-Type: application/json' \
  -d '{"text":"0123456789abcdefghij",
       "annotations":[{"id":"a","start":0,"end":4,"label":"甲"},
                      {"id":"b","start":3,"end":7,"label":"乙"},
                      {"id":"c","start":7,"end":10,"label":"丙"},
                      {"id":"d","start":15,"end":18,"label":"丁"}]}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

# alice 按批注 id 领取其所在整组 {a, b}（c 与 b 相接，不在组内）
curl -s -X POST http://localhost:8000/api/copies/$CID/annotations/a/claim \
  -H 'Content-Type: application/json' -d '{"reviewer":"alice"}'

# bob 领取 alice 持有的组 → 409 {"detail":{"message":...,"holder":"alice",...}}
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  http://localhost:8000/api/copies/$CID/annotations/b/claim \
  -H 'Content-Type: application/json' -d '{"reviewer":"bob"}'   # 409

# bob 领取无人持有的 c；alice 持有者释放 {a, b}
curl -s -o /dev/null -X POST http://localhost:8000/api/copies/$CID/annotations/c/claim \
  -H 'Content-Type: application/json' -d '{"reviewer":"bob"}'
curl -s -o /dev/null -X POST http://localhost:8000/api/copies/$CID/annotations/b/release \
  -H 'Content-Type: application/json' -d '{"reviewer":"alice"}'

# 仅持有者可 PATCH 单条范围与 label；与他人批注仅相接（end==start）允许
curl -s -X PATCH http://localhost:8000/api/copies/$CID/annotations/c \
  -H 'Content-Type: application/json' \
  -d '{"reviewer":"bob","start":8,"end":10,"label":"丙改"}'

# 读取同一提交状态（含 holder 与连通组），可直接喂给原审阅接口
curl -s http://localhost:8000/api/copies/$CID
```

## 快速开始（容器一键拉起）

```bash
docker compose up --build
# 或：docker build -t annot-migrate . && docker run --rm -p 8000:8000 annot-migrate
```

启动后访问本地 URL：**http://localhost:8000**（批注迁移）、**http://localhost:8000/arbitrate**（批注集仲裁）、**http://localhost:8000/sequence**（序列标注语料导出）、**http://localhost:8000/nesting**（嵌套批注审计）、**http://localhost:8000/units**（批注偏移制式转换）、**http://localhost:8000/review**（批注审阅切片）与 **http://localhost:8000/relabel**（批注标签体系迁移）。

七个页面均可点击「载入示例」一键填充演示数据：迁移页覆盖全部分支（平移 / 唯一命中 / 上下文胜出 / 并列待确认 / 无命中待确认）；
仲裁页覆盖全部分组形态（自动通过 / 一对一冲突 / 一对多 / 多对多 / 仅左侧 / 仅右侧）；
序列标注页覆盖切分与标注各分支（汉字串与 ASCII 串切分、批注边界切开 token、重叠批注分层、U / B-I-L 标签、辅助平面字符）；
嵌套审计页覆盖嵌套建树与 label 规则错误（严格包含父子、端点相接并列、根 / 子 label 规则违反）；
制式转换页源制式预置为 utf16，覆盖汉字、分解组合序列与辅助平面字符的三套坐标，并各含一条落入代理对内部与越界的非法批注；
审阅切片页覆盖装箱与窗口各分支（多条批注合并装箱、超跨度封口、窗口贴齐左边界、右侧边界余量转给左侧、辅助平面字符按码点计长）；
标签体系迁移页覆盖求值各分支（多步链、pattern 条件分流、回退规则、标签合并、循环与缺规则错误、已是目标标签）。

## 最小输入示例

页面三个输入框分别对应：旧版文本、新版文本、批注 JSON。

- 旧版文本：`第一段：旧版说明。第二段：保持不变。`
- 新版文本：`第一段：全新的新版说明。第二段：保持不变。`
- 批注 JSON：

```json
[{"id": "a1", "start": 13, "end": 17}]
```

`旧版[13, 17)` 即“保持不变”。点击「开始迁移」后该批注完整落在匹配块内，状态为 `shifted`，新偏移为 `[16, 20)`。直接「导出迁移结果」得到：

```json
[
  { "id": "a1", "start": 16, "end": 20, "status": "shifted" }
]
```

## 仲裁最小输入示例

打开 **http://localhost:8000/arbitrate**，三个输入框分别对应：文本、左侧批注 JSON、右侧批注 JSON。

- 文本：`目标是统一口径。`
- 左侧批注：`[{"id": "L1", "start": 0, "end": 4, "label": "目标"}]`
- 右侧批注：`[{"id": "R1", "start": 0, "end": 4, "label": "重点"}]`

`[0, 4)` 即“目标是统”。两侧范围重叠但不完全一致（label 不同）→ 构成一个待仲裁组。
选择「整组采用左侧」后「导出仲裁结果」得到：

```json
{
  "annotations": [
    { "id": "ann-1", "start": 0, "end": 4, "label": "目标", "source": "left", "source_id": "L1" }
  ],
  "decisions": [
    { "group_id": "g1", "left_ids": ["L1"], "right_ids": ["R1"], "decision": "left" }
  ]
}
```

若右侧 label 也是 `目标`，则该组一左一右且范围、label 完全相同，自动通过，无需决策。

## 序列标注最小输入示例

打开 **http://localhost:8000/sequence**，两个输入框分别对应：文本、批注 JSON。

- 文本：`数据采集ABC。`
- 批注 JSON：

```json
[
  { "id": "a1", "start": 0, "end": 4, "label": "流程" },
  { "id": "a2", "start": 1, "end": 3, "label": "核心" },
  { "id": "a3", "start": 4, "end": 8, "label": "短语" }
]
```

基础切分得到 `数据采集 [0,4)`、`ABC [4,7)`、`。 [7,8)`；`a2` 的边界把 `数据采集` 切开为
`数 [0,1)`、`据采 [1,3)`、`集 [3,4)`。`a1` 与 `a2` 重叠不能同层；`a3` 与二者均不重叠，
按（结束位置、层号）选最小进入第 2 层。点击「生成预览」后「下载 CoNLL」得到：

```
数 0 1 B-流程 O
据采 1 3 I-流程 U-核心
集 3 4 L-流程 O
ABC 4 7 O B-短语
。 7 8 O L-短语
```

## 嵌套审计最小输入示例

打开 **http://localhost:8000/nesting**，三个输入框分别对应：文本、批注 JSON、结构规则 JSON。

- 文本：`目标：统一口径。`
- 批注 JSON：

```json
[
  { "id": "p", "start": 0, "end": 8, "label": "段落" },
  { "id": "s", "start": 3, "end": 7, "label": "短语" }
]
```

- 结构规则 JSON：

```json
{ "root_labels": ["段落"], "children": { "段落": ["短语"] } }
```

`p [0, 8)` 严格包含 `s [3, 7)` → `s` 是 `p` 的直接子级；根 label `段落` 在
`root_labels` 中、`短语` 在 `段落` 的 children 中，审计无错误。点击「开始审计」
后「导出嵌套 JSON」得到：

```json
[
  {
    "id": "p", "label": "段落", "start": 0, "end": 8,
    "children": [
      { "id": "s", "label": "短语", "start": 3, "end": 7, "children": [] }
    ]
  }
]
```

若两条批注部分交叉（如 `[0, 5)` 与 `[3, 7)`）则记 `crossing` 错误；范围完全相同
则记 `duplicate_span` 错误；存在任何错误时禁止导出（导出接口返回 **409**）。

## 制式转换最小输入示例

打开 **http://localhost:8000/units**，两个输入框分别对应：文本、批注 JSON；
下拉框选择 `source_unit`（utf16）与 `target_unit`（utf8_byte）。

- 文本：`汉字é🎉A`（`é` 为 `e` + U+0301 分解序列，共 6 码点）
- 批注 JSON：

```json
[
  { "id": "u1", "start": 0, "end": 2, "label": "汉字" },
  { "id": "u2", "start": 2, "end": 4, "label": "组合" },
  { "id": "u3", "start": 4, "end": 6, "label": "表情" }
]
```

预览中每条批注给出原文与三种坐标：`u3` 的“🎉”在 codepoint 为 `[4, 5)`、
utf16 为 `[4, 6)`（代理对）、utf8_byte 为 `[9, 13)`（4 字节）。「导出目标制式 JSON」得到：

```json
[
  { "id": "u1", "start": 0, "end": 6, "label": "汉字" },
  { "id": "u2", "start": 6, "end": 9, "label": "组合" },
  { "id": "u3", "start": 9, "end": 13, "label": "表情" }
]
```

若把 `u3` 的 `start` 改成 `5`（落入代理对内部），预览逐字段列出
`u3 / start / 5 / not_boundary`，且导出被禁止（接口返回 **409**）。
把这份 UTF-8 结果以 `source_unit=utf8_byte`、`target_unit=codepoint` 再转一次，
即精确回到 `[0, 2)`、`[2, 4)`、`[4, 5)`。

## 审阅切片最小输入示例

打开 **http://localhost:8000/review**，两个输入框分别对应：文本、批注 JSON；
数字框填 `max_chars=10`、`context_chars=2`。

- 文本：`目标是统一口径，验收时复核样本。`（16 码点）
- 批注 JSON：

```json
[
  { "id": "a1", "start": 0, "end": 4, "label": "目标" },
  { "id": "a2", "start": 13, "end": 15, "label": "对象" }
]
```

`a2` 加入 `a1` 所在组后跨度为 14 > 10 → 封口为两个切片。`frag-1` 包住 `[0, 4)`，
剩余容量 6 均分（每侧封顶 2），左侧触及文本起点的余量转给右侧（仍封顶 2），
窗口为 `[0, 6)`；`frag-2` 包住 `[13, 15)`，窗口为 `[11, 16)`。点击「生成切片」
后「导出 JSONL」得到（每行一个切片，批注为窗口局部坐标）：

```jsonl
{"fragment_id": "frag-1", "text": "目标是统一口", "start": 0, "end": 6, "sha256": "d1b29c0590fd88da00c9471206aae9c2b62c0c094505bcf9fb9d3f7a9fb35a05", "annotations": [{"id": "a1", "start": 0, "end": 4, "label": "目标"}]}
{"fragment_id": "frag-2", "text": "复核样本。", "start": 11, "end": 16, "sha256": "db83cb00b53a4b7d93532ffb417e4ad2d3b8addc7f1b4ff471ca81297593fd41", "annotations": [{"id": "a2", "start": 2, "end": 4, "label": "对象"}]}
```

审阅方逐条给出决策后回导：`a1` 保留、`a2` 在 `frag-2` 窗口内修正为局部
`[1, 4)`（即全局 `[12, 15)`）：

```json
[
  { "fragment_id": "frag-1", "sha256": "d1b29c…5a05",
    "decisions": [ { "id": "a1", "action": "keep" } ] },
  { "fragment_id": "frag-2", "sha256": "db83cb…fd41",
    "decisions": [ { "id": "a2", "action": "correct", "start": 1, "end": 4 } ] }
]
```

「导入审阅结果」后后端重新切片并校验 fragment_id、摘要与归属，换回全局坐标：

```json
[
  { "id": "a1", "start": 0, "end": 4, "label": "目标", "action": "keep", "fragment_id": "frag-1" },
  { "id": "a2", "start": 12, "end": 15, "label": "对象", "action": "correct", "fragment_id": "frag-2" }
]
```

若摘要被改动、`id` 不属于该切片、同一 `id` 提交多次或有 `id` 未提交，
导入接口返回 **422** 并列出全部错误，整批拒绝。

## 标签体系迁移最小输入示例

打开 **http://localhost:8000/relabel**，三个输入框分别对应：文本、批注 JSON、规则 JSON。

### 条件分流示例

同一旧 label 按批注原文分流到不同目标。文本 `数据清洗`，批注 JSON：

```json
[
  { "id": "a", "start": 0, "end": 2, "label": "旧-对象" },
  { "id": "b", "start": 2, "end": 4, "label": "旧-对象" }
]
```

规则 JSON：

```json
{
  "target_labels": ["实体", "流程"],
  "rules": [
    { "from": "旧-对象", "to": "实体", "pattern": "数据" },
    { "from": "旧-对象", "to": "流程" }
  ]
}
```

`a` 的原文「数据」被 `pattern` 整个命中（`re.fullmatch`）→ `实体`；`b` 的原文「清洗」
未命中 → 落到排在该 `from` 最后的回退规则 → `流程`。点击「开始迁移」后「导出迁移结果
JSON」得到：

```json
[
  { "id": "a", "start": 0, "end": 2, "label": "实体" },
  { "id": "b", "start": 2, "end": 4, "label": "流程" }
]
```

### 标签合并示例

多个旧 label 汇入同一目标 label。文本 `样本统一`，批注 JSON：

```json
[
  { "id": "m1", "start": 0, "end": 2, "label": "旧-甲" },
  { "id": "m2", "start": 2, "end": 4, "label": "旧-乙" }
]
```

规则 JSON（`target_labels` 为 `["实体"]`）：

```json
{
  "target_labels": ["实体"],
  "rules": [
    { "from": "旧-甲", "to": "实体" },
    { "from": "旧-乙", "to": "实体" }
  ]
}
```

两条批注迁移后 label 均为 `实体`，`id`、范围与其余字段不变。

### 循环示例

文本 `口径`，批注 JSON：`[{"id": "c", "start": 0, "end": 2, "label": "坏-甲"}]`，
规则 JSON：

```json
{
  "target_labels": ["实体"],
  "rules": [
    { "from": "坏-甲", "to": "坏-乙" },
    { "from": "坏-乙", "to": "坏-甲" }
  ]
}
```

迁移链为 `坏-甲 → 坏-乙 → 坏-甲`，重复经过 `坏-甲` → 记 `cycle` 逐项错误，页面标红且
**禁止导出**（导出接口返回 **409**）。自环规则 `{ "from": "坏", "to": "坏" }` 同样
立即构成循环；label 不在 `target_labels` 且没有可用规则时记 `no_rule` 错误。

## 遮盖导出最小输入示例

遮盖导出为纯 HTTP 接口（无前端页面）。文本
`张三电话13800001234；工号E0088已核验。`（码点偏移从 0 计）：

| 批注 | 区间 | label | 说明 |
| --- | --- | --- | --- |
| `a1` | `[0, 2)` | 姓名 | 「张三」，命中 |
| `a2` | `[2, 15)` | 电话 | 「电话13800001234」，与 a1 **相接** → 合并为一个遮盖区 |
| `a3` | `[16, 23)` | 工号 | 「工号E0088」，命中（`[15,16)` 的「；」不在遮盖区内） |
| `a4` | `[0, 27)` | 记录 | 非命中，**跨两个遮盖区** → 拆成「；」「已核验。」两个片段 |
| `a5` | `[23, 27)` | 备注 | 非命中，完整可见 |

请求 JSON：

```json
{
  "text": "张三电话13800001234；工号E0088已核验。",
  "redact_labels": ["姓名", "电话", "工号"],
  "annotations": [
    {"id": "a1", "start": 0, "end": 2, "label": "姓名"},
    {"id": "a2", "start": 2, "end": 15, "label": "电话"},
    {"id": "a3", "start": 16, "end": 23, "label": "工号"},
    {"id": "a4", "start": 0, "end": 27, "label": "记录"},
    {"id": "a5", "start": 23, "end": 27, "label": "备注"}
  ]
}
```

相接的 `a1` 与 `a2` 合并成**一个** `[遮盖]`；`a4` 跨两个遮盖区拆成两个片段
（`fragment` 从 1 开始），命中的 `a1` / `a2` / `a3` 不出现在结果中：

```json
{
  "text": "[遮盖]；[遮盖]已核验。",
  "annotations": [
    {"source_id": "a4", "fragment": 1, "start": 4, "end": 5, "label": "记录"},
    {"source_id": "a4", "fragment": 2, "start": 9, "end": 13, "label": "记录"},
    {"source_id": "a5", "fragment": 1, "start": 9, "end": 13, "label": "备注"}
  ],
  "count": 3,
  "redacted_count": 2
}
```

`text[4:5]` 为「；」、`text[9:13]` 为「已核验。」——片段范围只覆盖原文可见部分。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/migrate` | 入参 `{old_text, new_text, annotations:[{id,start,end}]}`，返回每条批注的状态、新偏移与候选 |
| POST | `/api/export` | 同上加 `decisions`（`{"批注id": {"action":"choose","start":…,"end":…}}` 或 `{"action":"abandon"}`）；仍有未处理待确认项时返回 **409** 并列出 `pending_ids` |
| POST | `/api/arbitrate` | 入参 `{text, left:[{id,start,end,label}], right:[…]}`，返回仲裁组 `groups`（含组号、`auto`/`pending` 状态与两侧批注） |
| POST | `/api/arbitrate/export` | 同上加 `decisions`（`{"组号": "left"｜"right"｜"discard"}`）；后端重新分组并校验决策，仍有未处理组时返回 **409** 并列出 `pending_groups`，决策非法（未知组号 / 未知取值 / 对已自动通过的组做决策）返回 **422** |
| POST | `/api/sequence/preview` | 入参 `{text, annotations:[{id,start,end,label}]}`，返回 `{layers, tokens:[{text,start,end,tags}]}`（每 token 的各层 BILOU 标签） |
| POST | `/api/sequence/export` | 同上，返回 `{conll, layers, token_count}`，`conll` 为 UTF-8 CoNLL 文本（每行 `token start end 各层标签`）；非法区间 / 空 label / 重复 id / 批注只覆盖空白均返回 **422** |
| POST | `/api/nesting/preview` | 入参 `{text, annotations:[{id,start,end,label}], rules:{root_labels:[…], children:{父label:[子label…]}}}`，返回 `{errors, tree}`（`errors` 含 `duplicate_span` / `crossing` / `root_label` / `child_label`）；存在 `duplicate_span` / `crossing` 时 `tree` 为 `null` |
| POST | `/api/nesting/export` | 同上；后端重新计算审计，存在错误时返回 **409** 并列出 `errors`，否则返回 `{tree, count}`——保留原 id / label / 范围的稳定嵌套 JSON；非法区间 / 空 label / 重复 id / 规则列表含重复 label 均返回 **422** |
| POST | `/api/units/preview` | 入参 `{text, annotations:[{id,start,end,…}], source_unit, target_unit}`（制式取 `codepoint` / `utf16` / `utf8_byte`），返回 `{items:[{id,text,coordinates:{codepoint,utf16,utf8_byte},errors}], errors, lengths}`——每条批注原文与三种制式坐标；非法批注 `text`、`coordinates` 为 `null`，错误逐项列出（`out_of_range` / `not_boundary` / `bad_interval`，含 id、字段、值）；重复 id 返回 **422** |
| POST | `/api/units/export` | 同上；后端重新遍历文本、重建边界映射并重算，存在非法批注时返回 **409** 并列出 `errors`，否则返回 `{annotations, source_unit, target_unit, count}`——仅 `start`、`end` 转为目标制式，保持输入顺序与其余字段不变 |
| POST | `/api/review/preview` | 入参 `{text, annotations:[{id,start,end,label}], max_chars, context_chars}`（`id` 为唯一字符串），返回 `{fragments:[{fragment_id,text,start,end,sha256,annotations(局部坐标)}], count}`；单条批注超过 `max_chars`、非法区间 / 空 label / 重复 id / 非法参数均返回 **422** |
| POST | `/api/review/export` | 同上，返回 `{jsonl, count}`——后端重新装箱切片，`jsonl` 为每行一个切片的 JSONL 文本（含稳定 `fragment_id`、片段、全局范围、SHA-256 与局部坐标批注） |
| POST | `/api/review/import` | 同上加 `reviews:[{fragment_id, sha256, decisions:[{id, action:"keep"｜"drop"｜"correct", start?, end?}]}]`；后端重新切片并校验 fragment_id、摘要与归属，要求每个 id 恰好提交一次、修正范围落在窗口内，任一错误返回 **422** 并列出 `errors`（整批拒绝），否则返回 `{annotations(全局坐标，含 action 与 fragment_id), dropped, count}` |
| POST | `/api/relabel/preview` | 入参 `{text, annotations:[{id,start,end,label,…}], rules:{target_labels:[…], rules:[{from,to,pattern?}]}}`，返回 `{items:[{id,start,end,text,source_label,final_label,chain,steps,status,errors}], errors, target_labels}`——逐条迁移链（含命中规则）与逐项错误（`no_rule` / `cycle`）；非法区间 / 空 label / 重复 id / 规则集非法（空或重复目标标签、空 from/to、非法正则、回退规则冲突）均返回 **422** |
| POST | `/api/relabel/export` | 同上；后端重新求值迁移链，存在逐项错误时返回 **409** 并列出 `errors`，否则返回 `{annotations, count}`——保持输入顺序与 id、范围、其余字段不变，只替换 label |
| POST | `/api/redact/export` | 入参 `{text, annotations:[{id,start,end,label}], redact_labels:[…]}`；命中标签的区间重叠或相接即合并，每区替换为一个 `[遮盖]`，命中批注不导出，其余批注跨区拆成可见片段（完全被遮盖的舍弃），返回 `{text, annotations:[{source_id,fragment,start,end,label}], count, redacted_count}`——片段按原批注顺序及原文位置排列，`fragment` 从 1 开始，范围内文字为原文可见部分；无命中时文本与批注范围不变；非法区间 / 空 label / 重复 id / 非法遮盖标签均返回 **422**（不返回部分结果） |
| POST | `/api/copies` | 可领取审阅工作副本：入参 `{text, annotations:[{id,start,end,label}]}`（唯一字符串 id、非空 label、码点 `[start,end)`），返回 **201** 与初始快照（无人持有、`revision=1`，持久化到 SQLite 挂卷）；非法输入 **422** |
| GET | `/api/copies/{id}` | 返回同一提交状态 `{id, text, revision, annotations:[{id,start,end,label,holder}], groups:[{annotation_ids,holder}]}`（正长度重叠传递连通、相接不连边）；未知副本 **404** |
| POST | `/api/copies/{id}/annotations/{ann_id}/claim` | 入参 `{reviewer}`（非空字符串），按批注 id 整组领取（本人重复领取幂等）；他人持有 **409**，非法 reviewer **422**，副本 / 批注不存在 **404** |
| POST | `/api/copies/{id}/annotations/{ann_id}/release` | 入参 `{reviewer}`，持有者本人整组释放；无人持有 / 他人持有 **409**，非法 reviewer **422** |
| PATCH | `/api/copies/{id}/annotations/{ann_id}` | 入参 `{reviewer,start,end,label}`，仅持有者改单条范围与 label（不增删批注）后重算分组：并入的未领取批注归本人，触及他人 **409**（范围与持有权均不变），拆开的组保留原持有者；未持有 **409**，非法区间 / 空 label / 非法 reviewer **422**；并发请求等价逐次完整执行 |

迁移导出条目状态取值：`shifted` / `matched` / `disambiguated` / `manual` / `abandoned`（废弃项 `start`、`end` 为 `null`）。

```bash
curl -s http://localhost:8000/api/migrate \
  -H 'Content-Type: application/json' \
  -d '{"old_text":"第一段：旧版说明。第二段：保持不变。",
       "new_text":"第一段：全新的新版说明。第二段：保持不变。",
       "annotations":[{"id":"a1","start":13,"end":17}]}'
```

```bash
# 仲裁：分组
curl -s http://localhost:8000/api/arbitrate \
  -H 'Content-Type: application/json' \
  -d '{"text":"目标是统一口径。",
       "left":[{"id":"L1","start":0,"end":4,"label":"目标"}],
       "right":[{"id":"R1","start":0,"end":4,"label":"重点"}]}'

# 仲裁：整组采用左侧后导出
curl -s http://localhost:8000/api/arbitrate/export \
  -H 'Content-Type: application/json' \
  -d '{"text":"目标是统一口径。",
       "left":[{"id":"L1","start":0,"end":4,"label":"目标"}],
       "right":[{"id":"R1","start":0,"end":4,"label":"重点"}],
       "decisions":{"g1":"left"}}'

# 序列标注：预览分层 BILOU 标签
curl -s http://localhost:8000/api/sequence/preview \
  -H 'Content-Type: application/json' \
  -d '{"text":"数据采集ABC。",
       "annotations":[{"id":"a1","start":0,"end":4,"label":"流程"},
                      {"id":"a2","start":1,"end":3,"label":"核心"},
                      {"id":"a3","start":4,"end":8,"label":"短语"}]}'

# 序列标注：导出 CoNLL 文本
curl -s http://localhost:8000/api/sequence/export \
  -H 'Content-Type: application/json' \
  -d '{"text":"数据采集ABC。",
       "annotations":[{"id":"a1","start":0,"end":4,"label":"流程"},
                      {"id":"a2","start":1,"end":3,"label":"核心"},
                      {"id":"a3","start":4,"end":8,"label":"短语"}]}'

# 嵌套审计：检查错误并预览嵌套树
curl -s http://localhost:8000/api/nesting/preview \
  -H 'Content-Type: application/json' \
  -d '{"text":"目标：统一口径。",
       "annotations":[{"id":"p","start":0,"end":8,"label":"段落"},
                      {"id":"s","start":3,"end":7,"label":"短语"}],
       "rules":{"root_labels":["段落"],"children":{"段落":["短语"]}}}'

# 嵌套审计：无错误时导出稳定嵌套 JSON
curl -s http://localhost:8000/api/nesting/export \
  -H 'Content-Type: application/json' \
  -d '{"text":"目标：统一口径。",
       "annotations":[{"id":"p","start":0,"end":8,"label":"段落"},
                      {"id":"s","start":3,"end":7,"label":"短语"}],
       "rules":{"root_labels":["段落"],"children":{"段落":["短语"]}}}'

# 偏移制式转换：预览（源为 UTF-16，含落入代理对内部的非法批注）
curl -s http://localhost:8000/api/units/preview \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{"text":"汉字é🎉A",
 "source_unit":"utf16","target_unit":"utf8_byte",
 "annotations":[{"id":"u1","start":0,"end":2,"label":"汉字"},
                {"id":"u4","start":5,"end":6,"label":"坏边界"}]}
JSON

# 偏移制式转换：导出（后端重算；start、end 转 UTF-8 字节，顺序与其余字段不变）
curl -s http://localhost:8000/api/units/export \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{"text":"汉字é🎉A",
 "source_unit":"utf16","target_unit":"utf8_byte",
 "annotations":[{"id":"u1","start":0,"end":2,"label":"汉字"},
                {"id":"u2","start":2,"end":4,"label":"组合"},
                {"id":"u3","start":4,"end":6,"label":"表情"}]}
JSON

# 审阅切片：预览装箱切片（窗口、SHA-256 与局部坐标批注）
curl -s http://localhost:8000/api/review/preview \
  -H 'Content-Type: application/json' \
  -d '{"text":"目标是统一口径，验收时复核样本。",
       "max_chars":10,"context_chars":2,
       "annotations":[{"id":"a1","start":0,"end":4,"label":"目标"},
                      {"id":"a2","start":13,"end":15,"label":"对象"}]}'

# 审阅切片：导出 JSONL（后端重新装箱，每行一个切片）
curl -s http://localhost:8000/api/review/export \
  -H 'Content-Type: application/json' \
  -d '{"text":"目标是统一口径，验收时复核样本。",
       "max_chars":10,"context_chars":2,
       "annotations":[{"id":"a1","start":0,"end":4,"label":"目标"},
                      {"id":"a2","start":13,"end":15,"label":"对象"}]}'

# 审阅切片：导入审阅结果（a1 保留，a2 在 frag-2 窗口内修正为局部 [1,4)）
curl -s http://localhost:8000/api/review/import \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{"text":"目标是统一口径，验收时复核样本。",
 "max_chars":10,"context_chars":2,
 "annotations":[{"id":"a1","start":0,"end":4,"label":"目标"},
                {"id":"a2","start":13,"end":15,"label":"对象"}],
 "reviews":[{"fragment_id":"frag-1",
             "sha256":"d1b29c0590fd88da00c9471206aae9c2b62c0c094505bcf9fb9d3f7a9fb35a05",
             "decisions":[{"id":"a1","action":"keep"}]},
            {"fragment_id":"frag-2",
             "sha256":"db83cb00b53a4b7d93532ffb417e4ad2d3b8addc7f1b4ff471ca81297593fd41",
             "decisions":[{"id":"a2","action":"correct","start":1,"end":4}]}]}
JSON
```

```bash
# 标签体系迁移：预览迁移链（条件分流：「数据」→ 实体，「清洗」→ 回退到流程）
curl -s http://localhost:8000/api/relabel/preview \
  -H 'Content-Type: application/json' \
  -d '{"text":"数据清洗",
       "annotations":[{"id":"a","start":0,"end":2,"label":"旧-对象"},
                      {"id":"b","start":2,"end":4,"label":"旧-对象"}],
       "rules":{"target_labels":["实体","流程"],
                "rules":[{"from":"旧-对象","to":"实体","pattern":"数据"},
                         {"from":"旧-对象","to":"流程"}]}}'

# 标签体系迁移：导出（后端重新求值，只替换 label，顺序与其余字段不变）
curl -s http://localhost:8000/api/relabel/export \
  -H 'Content-Type: application/json' \
  -d '{"text":"数据清洗",
       "annotations":[{"id":"a","start":0,"end":2,"label":"旧-对象"},
                      {"id":"b","start":2,"end":4,"label":"旧-对象"}],
       "rules":{"target_labels":["实体","流程"],
                "rules":[{"from":"旧-对象","to":"实体","pattern":"数据"},
                         {"from":"旧-对象","to":"流程"}]}}'
```

```bash
# 遮盖导出：相接遮盖区合并（姓名+电话 → 一个 [遮盖]），跨区批注 a4 拆成两个片段
curl -s http://localhost:8000/api/redact/export \
  -H 'Content-Type: application/json' \
  -d '{"text":"张三电话13800001234；工号E0088已核验。",
       "redact_labels":["姓名","电话","工号"],
       "annotations":[{"id":"a1","start":0,"end":2,"label":"姓名"},
                      {"id":"a2","start":2,"end":15,"label":"电话"},
                      {"id":"a3","start":16,"end":23,"label":"工号"},
                      {"id":"a4","start":0,"end":27,"label":"记录"},
                      {"id":"a5","start":23,"end":27,"label":"备注"}]}'
```

## 本地开发（不用容器）

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
# 打开 http://127.0.0.1:8000 （迁移）、/arbitrate （仲裁）、/sequence （序列标注）、/nesting （嵌套审计）、/units （制式转换）、/review （审阅切片）或 /relabel （标签体系迁移）
```

## 测试

```bash
python -m pytest tests/
```

## 项目结构

```
app/
  main.py            # FastAPI 入口与接口（迁移 + 仲裁 + 序列标注 + 嵌套审计 + 制式转换 + 审阅切片 + 标签体系迁移 + 遮盖导出 + 可领取审阅工作副本）
  validation.py      # 批注输入校验：策略驱动的入口验证器（文本长度来源 / 码点区间 / 非空 label / id 类型与唯一性作用域 / 额外字段保留 / 错误前缀）
  migration.py       # 迁移核心：匹配块平移、原文搜索、上下文 Levenshtein 裁决
  arbitration.py     # 仲裁核心：二分图连通分量分组、整组决策校验与导出
  sequence.py        # 序列标注核心：token 切分、批注边界切开、贪心分层、BILOU 与 CoNLL 生成
  nesting.py         # 嵌套审计核心：区间关系检查、栈式建树、label 规则校验与稳定嵌套 JSON 导出
  units.py           # 制式转换核心：一次遍历建立三套字符边界映射、边界校验与偏移换算
  review.py          # 审阅切片核心：顺序装箱、窗口均分与边界余量转移、JSONL 导出、审阅结果校验回导
  relabel.py         # 标签体系迁移核心：规则集校验（目标标签 / 正则 / 回退约束）、迁移链求值（循环与缺规则检测）、导出重算
  redaction.py       # 遮盖导出核心：命中区间重叠/相接合并、[遮盖] 替换、可见片段拆分与新码点坐标换算
  copies.py          # 可领取审阅工作副本：正长度重叠传递分组（相接不连边）、整组领取/释放、持有者改单条后重算分组，SQLite 持久化（进程锁串行化，重启保留）
  static/            # 原生 HTML/CSS/JS 前端（index.html 迁移页、arbitrate.html 仲裁页、sequence.html 序列标注页、nesting.html 嵌套审计页、units.html 制式转换页、review.html 审阅切片页、relabel.html 标签体系迁移页）
tests/
  test_migration.py
  test_arbitration.py
  test_sequence.py
  test_nesting.py
  test_units.py
  test_review.py
  test_relabel.py
  test_redaction.py
  test_copies.py
  test_copy_store.py
  test_copy_api.py
  test_validation.py
Dockerfile
docker-compose.yml
requirements.txt
```
