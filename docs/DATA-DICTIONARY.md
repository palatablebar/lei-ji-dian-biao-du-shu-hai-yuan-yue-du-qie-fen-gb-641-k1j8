# 数据定义 meterback

本文件定义每个数字、时间、键的含义。页面只显示字段名与数值，解释在这里。

## 1 读数文件列契约

| 规范列名 | 可接受别名 | 类型 | 单位 | 可空 | 说明 |
|---|---|---|---|---|---|
| `meter_id` | 表号 / 电能表 / 电表编号 / meter | 字符串 | — | 否 | 前导零保形，`007` ≠ `7` |
| `register` | 寄存器 / 分量 / 费率 | 枚举 | — | 否 | `TOTAL` 总 / `PEAK` 峰 / `FLAT` 平 / `VALLEY` 谷，也接受 总/峰/平/谷 |
| `read_at` | 抄表时间 / 抄见时间 / 时间 | 时间 | — | 否 | 带偏移按偏移换算；无偏移按 +08:00；纯日期取当日 00:00 |
| `reading` | 读数 / 示数 / 累计读数 | 十进制 | 见表内 `unit` | 否 | 累计示数，不是用量 |
| `unit` | 单位 / 计量单位 | 枚举 | — | 是 | `kWh` / `度` / `千瓦时` / `MWh` / `兆瓦时`；空则取契约单位 |
| `read_type` | 抄表类型 / 类型 / 方式 | 枚举 | — | 是 | `N`/`正常`/`实抄`、`E`/`估抄`/`估算`；空按 `N` |
| `note` | 备注 / 说明 | 字符串 | — | 是 | 原样保存，只作证据 |

`meter_id / register / read_at / reading` 四列缺失即整文件失败（`E_HEADER_INVALID`）。

## 2 表计契约（meters.csv）

| 列 | 含义 | 约束 |
|---|---|---|
| `capacity` | 满量（寄存器最大值） | > 0；翻转判定的基准 |
| `multiplier` | 综合倍率 | > 0；区间用量 = 差值 × 倍率 |
| `unit` | 契约单位 | 只能是 `kWh` / `MWh`；读数先折到它 |
| `registers` | 寄存器组合，`;` 分隔 | 子集 of TOTAL/PEAK/FLAT/VALLEY |
| `replaced_at` | 换表时点 | 带偏移；与 `replaced_by` 成对出现 |
| `replaced_by` | 新表号 | 必须存在于契约 |

主寄存器：有 `TOTAL` 取 `TOTAL`，否则取第一个分量。区间账与月度账只统计主寄存器，避免与峰平谷重复计数。

## 3 数值与精度

- 用量：`Decimal`，单位 kWh，最小刻度 `0.001`；量化只发生在区间用量、月度分摊、分量闭合三处。
- 金额：`Decimal`，单位元，最小刻度 `0.01`；量化只发生在账单合计与分项补差两处。
- 电价：元/kWh，`prices.csv`（`TOTAL 0.65`、`PEAK 1.05`、`FLAT 0.65`、`VALLEY 0.32`）。
- 对外 JSON 与 CSV：用量三位小数、金额两位小数，全部以字符串输出，禁止 float 往返。

## 4 时间与分桶

- 入库一律 UTC（`+00:00`）；页面展示与月桶按 `Asia/Shanghai (+08:00)`。
- 区间：`[起, 止)`，允许跨月、跨年；上一段止点等于下一段起点。
- 月桶：`[月初 00:00, 下月初 00:00)`，按覆盖天数加权；天数用秒数 / 86400 折算，保留 9 位小数。
- 相邻区间不重不漏由「半开 + 首尾相接」保证，测试 `test_intervals_are_half_open_and_contiguous` 固定。

## 5 空值与三态

- 哨兵清单：`-`、`--`、`—`、`/`、`N/A`、`null`、`未知`、`无`、`暂无`、`待定`、`空`、`未抄`、空串。
- `null`（无值）、`0`（有效零读数）、空串（格式缺陷）语义不同：前两者走不同原因码，空串按哨兵处理。
- 文件级三态：0 字节 → `E_EMPTY_FILE`；仅表头 → `E_HEADER_ONLY`；数据行全空 → `E_ALL_BLANK`。

## 6 去重与冲突键

- 业务键：`(meter_id, register, read_at)`（时间已归一到 UTC）。
- 同键多行且读数完全一致 → 保留行号最小的一条，其余记 `K_DUPLICATE_IDENTICAL`。
- 同键多行且读数不同 → 两条都进隔离区（`Q_DUPLICATE_CONFLICT`），带各自原值，不做「后写覆盖」。
- 不使用全行哈希：备注列变化不代表读数变化。

## 7 判定规则

| 判定 | 条件 | 结果 |
|---|---|---|
| 常规 | 本次 ≥ 上次 | 差值 = 本次 − 上次 |
| 满量翻转 | 本次 < 上次 且 上次 ≥ 满量 × 0.9 且 补满量后差值 ≤ 满量 | 差值 = 满量 − 上次 + 本次 |
| 倒走 | 本次 < 上次 且 不满足翻转前提 | `Q_REVERSE_READING`，整行隔离 |
| 换表末段 | 旧表最后一条读数（≤ 换表时点） | 区间止点 = 换表时点 |
| 新表首读 | 新表第一条读数 | 区间 = [换表时点, 首读时间)，用量从 0 起算 |
| 估抄 | `read_type = E` | 不作锚点、不参与单调判定，落在区间内时给区间打 `K_ESTIMATE_SKIPPED` |

## 8 尾差处理

1. 月度切分：按天数权重得到精确份额，各自向下取整后，余数按小数部分从大到小逐份补 0.001，保证每段之和等于区间用量。
2. 分量闭合：同月「总表 −（峰+平+谷）」若不等于 0，差额并入当月用量最大的分量（并列取 峰 > 平 > 谷），并标 `K_TOU_ADJUSTED`。
3. 账单闭合：账单额 = 先合计再四舍五入；分项金额各自向下截尾，差额按小数部分从大到小逐分补到分项，补完分项之和等于账单额，差额记 `adjust_cents`。

## 9 原因码

文件级 `E_*`：`E_EMPTY_FILE`、`E_HEADER_ONLY`、`E_ALL_BLANK`、`E_HEADER_INVALID`、`E_DECODE`、`E_SAMPLE_UNKNOWN`、`E_CONTRACT_INVALID`、`E_BATCH_NOT_FOUND`。

正式账 `E_*`：`E_LEDGER_NOT_PUBLISHED`（未发布正式账就更正）、`E_LEDGER_BOUND`（已有更正入账后换基准批次）、`E_VERSION_NOT_FOUND`、`E_POSTED_MONTH_CLOSED`（入账月已关账）、`E_CORRECTION_EMPTY`（更正行或原因为空 / 全部无法解析）、`E_CORRECTION_NOT_FOUND`、`E_RECONCILE_UNBALANCED`（更正后四账不平，未入账）。

行级 `Q_*`：`Q_COLUMN_COUNT`、`Q_TRUNCATED_ROW`、`Q_MISSING_READING`、`Q_BAD_READING`、`Q_BAD_TIME`、`Q_UNKNOWN_METER`、`Q_BAD_REGISTER`、`Q_BAD_UNIT`、`Q_BAD_READ_TYPE`、`Q_NEGATIVE_READING`、`Q_EXCEEDS_CAPACITY`、`Q_REVERSE_READING`、`Q_DUPLICATE_CONFLICT`、`Q_METER_RETIRED`。

标记 `K_*`：`K_ROLLOVER`、`K_REPLACEMENT_FIRST`、`K_ESTIMATE_SKIPPED`、`K_CHARSET_FALLBACK`、`K_BOM_STRIPPED`、`K_SEGMENT_SPLIT`、`K_TRUNCATED_TAIL`、`K_TOU_ADJUSTED`、`K_BILL_ROUNDED`、`K_UNIT_ASSUMED`、`K_UNIT_CONVERTED`、`K_DUPLICATE_IDENTICAL`、`K_SUPERSEDED`、`K_ADJUSTMENT_BOOKED`。

唯一来源：`app/reasons.py`；页面与导出的原因文案都从它取。

## 10 正式账与更正（M8）

### 入账规则

1. **关账发布**：`POST /api/ledger/publish` 把完成批次的账单与四账复制为正式账创世版本（`kind=genesis`）；同批次重复发布幂等。血统内已有更正入账后不得更换基准批次（`E_LEDGER_BOUND`）。
2. **更正身份**：`correction_id` = 基准批次 + 请求入账月（原文，可空）+ 原因 + 工单 + 更正行原文的内容摘要。同一 payload 永远命中同一个号，重跑幂等；默认入账月随版本推进不会让同一更正变成新更正。
3. **差额进开放账期**：已入账月（≤ 已发最大月份）的账单一行不改；更正重算的「应算 − 已入账」差额按寄存器拆成调账分项（`line_kind=adjustment`），记到 `posted_month`（默认已发最大月份 +1；指定时必须未关账）。已入账基数 = 当前版本的用量分项 + 当前版本全部调账分项（按起源月归集），第二次更正不会冲掉第一次的调账。
4. **调账不进用量**：调账分项的用量差只作证据备忘，不计入入账月用量；入账月用量仍由原始读数算出。
5. **noop**：版本内容摘要（账单 + 分项数值）与当前版本一致时不产生新版本，更正记 `status=noop`（如更正值与原值相同、更正行全部被隔离）。
6. **被隔离的更正行不顶替原行**：更正行若被质量规则隔离，被它取代的原读数自动回到有效集，逐轮重建直到稳定。
7. **取代链**：同业务键后到更正取代先到行（`supersedes` 记「批次行 N」或「cor-xxx#n」），原行全部留库，读数账单设「被取代」桶保持平衡。

### 版本级五账

读数账（含「被取代」桶）、区间账、月度账、账单账同批次口径；新增**调账账**：本版本调账分项合计 = 各关账月「应算 − 已入账」差额合计，两边逐分相等才允许发布。

### 新表

| 表 | 关键列 | 说明 |
|---|---|---|
| `ledger_versions` | `version_id` `version_no` `base_batch_id` `correction_id` `kind` `digest` `posted_month` | 账版本；`kind` = genesis / correction |
| `ledger_pointer` | `version_id` | 单行当前指针，只在发布事务里翻转 |
| `ledger_bills` | `version_id` `month` `meter_id` `usage_kwh` `amount_yuan` `kind` | 正式月账；`kind` = issued / carried / adjusted |
| `ledger_bill_lines` | `version_id` `line_kind` `register` `usage_kwh` `amount_yuan` `origin_month` `correction_id` | 分项：usage / adjustment |
| `ledger_reconcile` | `version_id` `json` | 版本级五账 |
| `corrections` | `correction_id` `base_batch_id` `reason` `posted_month` `status` `result_version_id` `payload_json` | 更正单；status = applied / noop / failed |
| `correction_readings` | `correction_id` `seq` `reading_text` `reading_norm` `status` `supersedes` | 更正读数行，追加存放不删改 |

发布事务 = 版本行 + 账单 + 分项 + 五账 + 指针 + 更正单，同一事务落库；任何一步失败整体回滚，当前版本不动，失败尝试以 `status=failed` 单独留痕，可原样重试。

