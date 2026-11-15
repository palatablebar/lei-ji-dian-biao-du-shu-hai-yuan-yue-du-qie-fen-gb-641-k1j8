# 运行手册 meterback

## 1 启动

```bash
cd projects/gb-641
docker compose up -d --build
curl -sS http://localhost:18200/api/health
```

页面：<http://localhost:18200/>。首次打开没有批次，选样例点「开始处理」即可跑通。

容器内端口 8000，宿主 18200；SQLite 落在命名卷 `meterback-data` 的 `/data/meterback.sqlite`。

## 2 主流程

1. 总览页选 `readings_dirty.csv`（脏样例，13 类问题），点「开始处理」。
2. 阶段卡显示七段「进 / 出 / 隔离」；任一阶段失败可点该卡的「重试」。
3. 读数诊断页看逐行诊断与隔离区，点「隔离区 CSV」下载证据。
4. 区间还原页看翻转、换表末段、新表首读与标记，可点「重算区间」。
5. 月度分摊页看每月合计、账单与尾差，可点「重算分摊」。
6. 批次对账页看四账与运行清单；导出页下载五份文件。

命令行同一套接口：

```bash
curl -sS -X POST http://localhost:18200/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"sample":"readings_dirty.csv"}'
curl -sS http://localhost:18200/api/reconcile | python3 -m json.tool
curl -sS -o /tmp/monthly.csv http://localhost:18200/api/export/monthly.csv
```

## 3 常见操作

- **同输入重跑**：再点一次「开始处理」或 POST 同一 `sample`，命中批次键，返回同一 `batch_id`，行数与合计不变。
- **强制重算**：POST `/api/batches/{batch_id}/retry`，用同一个 `sample` 重跑并覆盖该批次的行。
- **换样例**：下拉框里其余样例分别是干净、GBK 中文表头、末行截断、空文件、仅表头、全空行、表头缺列。
- **重跑测试**：`docker compose exec -T app python -m pytest -q`。
- **清库**：`docker compose down -v` 删除命名卷；下次启动自动建表。

## 3.1 关账与补发更正

1. **关账**：批次完成后，正式账页点「把当前批次发布为正式账」（或
   `curl -X POST http://localhost:18200/api/ledger/publish`），得到创世版本 v1；
   同一批次重复发布返回同一版本。
2. **提交更正**：正式账页填表（表号、寄存器、抄表时间、更正读数、原因），或：

   ```bash
   curl -sS -X POST http://localhost:18200/api/corrections \
     -H 'Content-Type: application/json' \
     -d '{"items":[{"meter_id":"M0001","register":"TOTAL",
                   "read_at":"2026-03-16T00:00:00+08:00","reading":"2600.0","unit":"kWh"}],
         "reason":"抄表班补发：3月16日抄错","ticket":"GD-1001"}'
   ```

   响应给出：入账月（默认已发最大月份的下一月）、受影响表计、区间判定依据、
   每个关账月的已入账 → 应算 → 差额、调账分项、版本级五账。
3. **复核**：`/ledger` 页看当前版本正式月账（调账分项带 ←起源月 标记）、
   更正复核、版本历史；`?version_id=` 可回看任何旧版本，导出跟随所选版本。
4. **重跑同一更正**：原样再 POST 一次，命中同一 `correction_id`，
   返回 `idempotent_replay: true`，不新增版本、不重复调账。
5. **失败排查**：`E_LEDGER_NOT_PUBLISHED` 先发布正式账；`E_POSTED_MONTH_CLOSED`
   换一个未关账的入账月；`E_RECONCILE_UNBALANCED` 看返回的差额账户，
   当前正式账保持上一版本，修正后原样重试即可（failed 记录会被同号覆盖）。


## 4 失败排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 422 `E_EMPTY_FILE` | 样例文件 0 字节 | 选非空样例 |
| 422 `E_HEADER_ONLY` | 只有表头 | 补数据行 |
| 422 `E_ALL_BLANK` | 数据行全空 | 检查导出是否只带格式 |
| 422 `E_HEADER_INVALID` | 缺 `meter_id/register/read_at/reading` | 按列别名表改名 |
| 422 `E_CONTRACT_INVALID` | `meters.csv` 重复表号、零倍率、换表目标缺失 | 修契约 |
| 400 `E_SAMPLE_UNKNOWN` | 样例名不在白名单 | 用页面下拉框里的名字 |
| 404 `E_BATCH_NOT_FOUND` | 还没有批次就导出 | 先跑一次处理 |
| 409 `E_LEDGER_NOT_PUBLISHED` | 还没有正式账就提交更正 | 先发布正式账 |
| 409 `E_LEDGER_BOUND` | 已有更正入账后换基准批次 | 更正只能跟随原血统 |
| 409 `E_POSTED_MONTH_CLOSED` | 入账月已关账 | 用开放账期月份 |
| 409 `E_RECONCILE_UNBALANCED` | 更正后四账不平 | 看差额账户，当前版本未动 |
| 四账「不平」 | 逻辑或数据被改 | 看差额字段与说明，逐账核对 |

日志：`docker compose logs -f app`（uvicorn 访问日志）。运行清单里的 `input_digest` 可用来确认两次运行读的是同一份输入。

## 5 复算

1. 从导出页取 `monthly.csv` 与 `manifest.json`；
2. `manifest.json` 的 `inputs[].sha256` 与样例文件比对，确认输入未变；
3. 逐月加总 `用量kWh` 应等于 `manifest.json` 的 `counters` 与 `accounts[1]`（区间账）两侧；
4. 账单 `金额元` 之和应等于 `accounts[3]` 两侧，分项之和等于账单额。

## 6 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `METERBACK_DB` | `/data/meterback.sqlite` | SQLite 路径 |
| `METERBACK_SAMPLE_DIR` | `/app/sample_data` | 样例目录 |

规则版本写在 `app/config.py` 的 `RULE_VERSION`，进入运行清单；改规则必须同时加测试，否则同输入重跑会出现两个批次。
