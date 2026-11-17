# 运行手册 gridlab

## 启动

```bash
docker compose up -d --build
curl -s http://127.0.0.1:18203/api/health
open http://127.0.0.1:18203/
```

首页点「导入样例」即可跑通主流程；批次与四账在 `/batches`。

## 重置批次库

```bash
docker compose down -v --remove-orphans
docker compose up -d --build
```

命名卷 `gridlab-data` 承载 `/app/data/gridlab.db`，删除卷即清空全部批次。

## 容器内测试

```bash
docker compose exec -T app python -m pytest -q
```

## 自带样例

| key | 文件 | 用途 |
|---|---|---|
| `telemetry_raw` | `sample_data/telemetry_raw.csv` | 主脏样例：编码 BOM、换行混用、重复键冲突、越界、哨兵、单位不符、迟到、偏离采样率、契约外传感器 |
| `gap_series` | `sample_data/gap_series.csv` | 2 桶线性补齐与 2 桶边缘缺锚的精确算术 |
| `midnight_edge` | `sample_data/midnight_edge.csv` | 跨日桶与 15 分钟窗口右端开 |
| `offset_overlap` | `sample_data/offset_overlap.csv` | 偏移区间重叠导致整行隔离 |
| `column_count` | `sample_data/column_count.csv` | 少列与多列 |
| `truncated_tail` | `sample_data/truncated_tail.csv` | 尾行截断 |
| `header_only` | `sample_data/header_only.csv` | 仅表头 |
| `blank_rows` | `sample_data/blank_rows.csv` | 全空行 |
| `crlf_quoted` | `sample_data/crlf_quoted.csv` | CRLF、引号内换行、重复表头分段 |
| `gb18030` | `sample_data/telemetry_gb18030.csv` | GB18030 与中文表头 |
| `empty` | `sample_data/empty_sample.csv` | 0 字节 |
| `dst_fallback` | `sample_data/dst_fallback.csv` | 秋季回拨：本地 02:00–03:00 两次经过，含 1 条须隔离的裸本地时间 |

偏移表有 `device_offsets.csv`（主样例，`AM-06` 故意留两段重叠区间）与 `dst_device_offsets.csv`（回拨样例）；回拨表固定用 `dst_berlin_fallback.csv`，其中 `*` 为全站条目。
导入回拨批次时 body 增加 `"dst": "berlin_fallback"`；不传 `dst` 即按旧规则运行，批次键与结论不变。

## 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| POST | `/api/samples/import` | 导入样例，body `{"sample": "...", "offsets": "device_offsets"}` |
| GET | `/api/samples` | 最近批次的采信元信息 |
| GET | `/api/timebase` | 传感器契约与偏移表 |
| GET | `/api/quarantine` | 隔离行与原因分布 |
| GET | `/api/quarantine.csv` | 隔离 CSV |
| GET | `/api/grid` | 网格点摘要 |
| POST | `/api/grid` | 从采纳行重新推导网格并逐点比对 |
| GET | `/api/gaps` | 缺口段与窗口覆盖 |
| POST | `/api/gaps/repair` | 从采纳行重新推导缺口并与落库比对 |
| GET | `/api/batches` | 批次清单 |
| GET | `/api/reconcile` | 五账 |
| GET | `/api/dst/timeline` | 回拨可追溯时间轴（本地墙钟、第几次经过、UTC 桶） |
| GET | `/api/dst/affected` | 与回拨跨度相交的 15 分钟窗口及回拨段内缺口 |
| GET | `/api/export/grid.csv` | 网格 CSV |
| GET | `/api/export/accepted.csv` | 采纳 CSV |
| GET | `/api/export/quarantine.csv` | 隔离 CSV |
| GET | `/api/export/manifest.json` | 运行清单 |
| GET | `/api/export/evidence` | 单元格证据，参数 `sensor_id` 与 `bucket` |

## 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| 409 `STATE_CONFLICT` | 偏移表被改动后与既有批次冲突 | 改回偏移表，或删除命名卷按新表重跑 |
| 400 `HEADER_MISSING` | 输入列名不在别名表内 | 在 `app/modules/m1_intake/sniff.py` 的别名表补一条 |
| 404 `RUN_NOT_FOUND` | 指定了不存在的批次号 | 省略 `run_id` 参数即取最近批次 |
| 四账「不平」 | 有人改了落库行 | 用 `POST /api/grid` 与 `POST /api/gaps/repair` 重新推导比对，差额在 `/batches` 可见 |
