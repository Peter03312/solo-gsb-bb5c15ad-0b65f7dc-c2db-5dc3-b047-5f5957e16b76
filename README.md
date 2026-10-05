# 园区访客通行规划服务（Park Route Planner）

面向园区访客的**离线**通行规划 HTTP 服务。导入一张带方向、通行秒数、台阶标记的
步道图，以及按**半开时间区间** `[start, end)` 生效的封闭记录；给定起终点、出发
时刻与是否避台阶，返回**最早到达**路线及逐段的进入、离开时间。支持在查询中
指定**按顺序停靠的站点**（每站带开始停留时间窗与停留秒数），见第 4.1 节。

- 语言/存储：Python 3.11 + SQLite（标准库，无需外部数据库）
- Web 框架：FastAPI + Uvicorn
- 无外部网络依赖，适合园区内网 / 边缘设备离线运行

---

## 1. 快速启动

### 方式 A：Docker Compose（推荐，一键启动）

```bash
docker compose up -d --build
# 查看状态
curl http://localhost:8080/health
```

容器启动时会自动建库并导入内置示例数据（`app/sample_data.py`），随后在
`8080` 端口提供服务。数据持久化在命名卷 `park-route-data` 中。

停止：

```bash
docker compose down            # 保留数据
docker compose down -v         # 连同数据卷一起删除
```

### 方式 B：本地 Python 运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 建库 + 初始化数据（默认内置示例；--graph/--closures 可指定自定义 JSON）
python -m app.cli init-db

# 启动服务
python -m app.cli serve
# 或：uvicorn app.main:app --host 0.0.0.0 --port 8080
```

> 直接用 `uvicorn` 启动时若库为空，服务可正常运行，只是查询会返回 `503 no_data`，
> 请改用导入接口写入数据，或使用 `python -m app.cli serve` / Docker 入口（会自动种子化）。

**服务地址与端口**：默认 `http://0.0.0.0:8080`，可用 `APP_HOST` / `APP_PORT` 修改。
交互式 API 文档：`http://localhost:8080/docs`。

---

## 2. 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `APP_PORT` | `8080` | HTTP 监听端口 |
| `APP_DB_PATH` | `./data/routing.db`（容器内 `/data/routing.db`） | SQLite 数据库文件路径 |
| `APP_SEED_GRAPH` | （空） | 首次初始化使用的步道图 JSON 文件路径 |
| `APP_SEED_CLOSURES` | （空） | 首次初始化使用的封闭记录 JSON 文件路径 |
| `APP_SEED_SAMPLE` | `1` | 未指定种子文件时，是否导入内置示例数据；`0`/`false` 表示空库启动 |

---

## 3. 数据模型

### 步道图

- **节点**：字符串标识，如 `"GATE"`、`"PLAZA"`。
- **路段（边）**：有方向；字段：
  - `id`：路段编号，**同一版本内唯一**，用于平局字典序比较；
  - `from` / `to`：起终点，必须引用已声明的节点；
  - `travel_seconds`：通行秒数，**正整数**；
  - `has_stairs`：是否含台阶（布尔）。

示例见 [`data/graph.example.json`](data/graph.example.json)。

### 封闭记录

- 作用于某条边，时间区间为**半开** `[start, end)`：
  - `end` 时刻该路段**立即恢复**，允许恰好在 `end` 进入；
  - `start` 时刻该路段已经封闭。
- 必须满足 `start < end`，时间使用 ISO 8601（推荐 `...Z`）；不带时区按 UTC 处理。

示例见 [`data/closures.example.json`](data/closures.example.json)。封闭记录为**整体替换**。

### 通行与等候规则

- 允许在节点等候封闭结束；
- 一条边的整个通行区间 `[enter, enter + travel_seconds)` 不得与该边任何封闭区间重叠，
  不允许“先走进路段、在路中间等”；
- 寻路时自动计算每段实际进入时刻与等候秒数。

---

## 4. HTTP API

所有请求/响应均为 JSON。

### `POST /api/route` — 查询路线

请求：

```json
{
  "origin": "GATE",
  "destination": "TOWER",
  "departure_time": "2026-10-05T09:55:00Z",
  "avoid_stairs": false
}
```

响应 `200`（节选）：

```json
{
  "origin": "GATE",
  "destination": "TOWER",
  "query_departure_time": "2026-10-05T09:55:00Z",
  "arrival_time": "2026-10-05T10:07:00Z",
  "total_waiting_seconds": 0,
  "avoid_stairs": false,
  "edge_sequence": ["E1", "E3", "E5"],
  "segments": [
    {
      "edge_id": "E1",
      "from_node": "GATE",
      "to_node": "PLAZA",
      "has_stairs": false,
      "enter_time": "2026-10-05T09:55:00Z",
      "leave_time": "2026-10-05T09:58:00Z",
      "travel_seconds": 180,
      "waiting_seconds": 0
    }
  ],
  "data_version": 2
}
```

- 每段都给出**进入**（`enter_time`，已含节点等候）与**离开**（`leave_time`）时刻，
  以及 `waiting_seconds`；
- `data_version` 标明本次查询实际使用的数据版本。

错误：

| 场景 | 状态码 | `error` |
| --- | --- | --- |
| 起终点未知、时间格式错误、停靠点参数非法等 | `400` | `invalid_payload` |
| 无可行路线（含避台阶后不可达、任一停靠站无法按时停留） | `404` | `no_route` |
| 尚未导入步道图 | `503` | `no_data` |

### 4.1 按顺序停靠（`stops`，可选）

查询可携带 `stops` 数组，访客将**按给定顺序**完成各站停靠：

```json
{
  "origin": "GATE",
  "destination": "TOWER",
  "departure_time": "2026-10-05T08:50:00Z",
  "avoid_stairs": false,
  "stops": [
    {
      "node": "LAKE",
      "earliest_start": "2026-10-05T09:00:00Z",
      "latest_start": "2026-10-05T09:10:00Z",
      "stay_seconds": 300
    }
  ]
}
```

每站字段：

| 字段 | 说明 |
| --- | --- |
| `node` | 停靠地点（必须是已知节点） |
| `earliest_start` | 最早开始停留时刻（ISO 8601 或纪元秒） |
| `latest_start` | 最晚开始停留时刻，须 `>= earliest_start`（闭区间） |
| `stay_seconds` | 停留秒数，**非负整数**（`0` 表示即停即走） |

语义规则：

- 抵达某站后可在节点**等到窗口开放**，开始停留时刻为
  `max(抵达时刻, earliest_start)`，且**不得晚于 `latest_start`**，
  否则本次查询无路线（`404 no_route`）；
- **结束停留后才能前往下一站**；途经尚未轮到的站点（包括未来各站所在
  节点）**不算完成停靠**，只当作普通节点通过；
- 起点本身就是第 1 站、或相邻两站位于同一节点时，立即（链式）完成停靠；
- 选路目标不变：**最终到达**（完成全部停靠并抵达终点）最早优先，平局按
  **全程**路段编号序列字典序选取，不逐段独立打破平局；
- 封闭区间（半开）、避台阶、单次查询固定数据版本的规则对每一程同样生效。

带停靠时响应在原有结构基础上增加 `stops` 明细与 `total_stay_seconds`
（无停靠点时不出现这两个字段，返回结构与之前完全一致）：

```json
{
  "arrival_time": "2026-10-05T09:09:00Z",
  "total_waiting_seconds": 120,
  "total_stay_seconds": 300,
  "edge_sequence": ["E1", "E3", "E5"],
  "segments": ["... 全程逐段（跨各程拼接） ..."],
  "stops": [
    {
      "node": "LAKE",
      "arrival_time": "2026-10-05T08:58:00Z",
      "start_time": "2026-10-05T09:00:00Z",
      "end_time": "2026-10-05T09:05:00Z",
      "stay_seconds": 300,
      "wait_for_window_seconds": 120
    }
  ],
  "data_version": 2
}
```

- `stops[i]` 给出第 `i+1` 站的**抵达**、**开始停留**、**结束停留**时刻
  （均附 `_epoch` 秒级副本）与等窗秒数；
- `total_waiting_seconds` 为非通行、非停留的等候总秒数（封闭等候 + 等窗）；
- `segments` 覆盖**全程**（各程拼接），`waiting_seconds` 仍为进入该路段前
  在节点的封闭等候。

错误映射：

| 场景 | 状态码 | `error` |
| --- | --- | --- |
| 时间窗无效（`earliest_start > latest_start`、时间格式错误） | `400` | `invalid_payload` |
| `stay_seconds` 非非负整数（负数、小数、布尔、字符串） | `400` | `invalid_payload` |
| 停靠地点未知 | `400` | `invalid_payload` |
| 任一站无法按时停留（抵达已晚于 `latest_start`） | `404` | `no_route` |
| 某站或终点不可达 | `404` | `no_route` |

### `POST /admin/graph` — 原子发布新步道图

```bash
curl -X POST http://localhost:8080/admin/graph \
  -H 'Content-Type: application/json' \
  -d @data/graph.example.json
```

校验失败（未知节点引用、非正通行秒数、编号重复等）返回 `400`，响应体含
`"rejected": true` 与当前仍生效的 `active_version`，**旧版本完整保留**。
发布成功后旧封闭记录中引用了已删除路段的条目会自动清理。

### `POST /admin/closures` — 原子整体替换封闭记录

```bash
curl -X POST http://localhost:8080/admin/closures \
  -H 'Content-Type: application/json' \
  -d @data/closures.example.json
```

引用不存在的路段、`start >= end` 等返回 `400` 并保留旧版本。

### 其他

- `GET /health`：健康检查与当前版本号；
- `GET /version`：当前版本及节点/路段/封闭数量。

---

## 5. 调用示例

```bash
# 09:55 出发（E4 在 10:00–11:00 封闭），不避台阶 → 绕行平路
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T09:55:00Z","avoid_stairs":false}'

# 避台阶：只经过 has_stairs=false 的路段
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T08:00:00Z","avoid_stairs":true}'

# 节点等候：HILL 09:59 出发，E4 [10:00,11:00) 封 → 在 HILL 等到 11:00 进入
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"HILL","destination":"TOWER",
       "departure_time":"2026-10-05T09:59:00Z","avoid_stairs":false}'

# 按顺序停靠：08:50 出发，LAKE 站 09:00–09:10 之间开始停留、停留 300 秒
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T08:50:00Z",
       "stops":[{"node":"LAKE",
                 "earliest_start":"2026-10-05T09:00:00Z",
                 "latest_start":"2026-10-05T09:10:00Z",
                 "stay_seconds":300}]}'
# 结果要点：E1(08:50→08:53) → E3(08:53→08:58) 抵 LAKE；
#   等窗 120 秒，09:00 开始停留，09:05 结束；E5(09:05→09:09)；
#   arrival_time=2026-10-05T09:09:00Z，stops[0] 记录 08:58/09:00/09:05。

# 停靠窗口不可行：最晚 08:55 开始，但最早 08:58 才到 LAKE → 404 no_route
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T08:50:00Z",
       "stops":[{"node":"LAKE",
                 "earliest_start":"2026-10-05T08:00:00Z",
                 "latest_start":"2026-10-05T08:55:00Z",
                 "stay_seconds":60}]}'

# 冒烟自检（需服务已启动）
python3 scripts/smoke_test.py http://localhost:8080
```

Python 示例：

```python
import json, urllib.request

req = urllib.request.Request(
    "http://localhost:8080/api/route",
    data=json.dumps({
        "origin": "GATE",
        "destination": "TOWER",
        "departure_time": "2026-10-05T09:55:00Z",
        "avoid_stairs": True,
    }).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as resp:
    route = json.load(resp)
print(route["arrival_time"], route["edge_sequence"])
```

---

## 6. 选路与版本语义

- **选路目标**：① 最早到达（带停靠时为完成全部停靠并抵达终点的时刻）；
  ② 到达时刻相同时，按**全程路段编号序列字典序**选路（字符串逐元素比较，
  例如 `("E1","E3") < ("E2","E4")`），不逐段独立打破平局。
- **算法**：
  1. 时间依赖的 Dijkstra（FIFO 网络）计算最早到达时刻 `T`（带停靠时逐程
     复合：各程最早到达映射与“停留结束时刻”均为单调非降，逐程取最早即
     全局最早，同时校验各站窗口可行）；
  2. 在“最终到达不晚于 `T`”的状态空间做多标签搜索，取 `(到达时刻, 编号序列)`
     最优者。无停靠时状态为节点；带停靠时状态为 `(节点, 已完成停靠数)`，
     抵达当前轮到的站点即完成停靠（等窗 + 停留），途经未来站点不产生停靠。
     第二阶段用于处理“节点等候/停靠窗口把不同路径的到达时刻拉平”后，
     单标号算法可能选错字典序路径的问题。
- **原子发布与版本一致**：
  - 每次导入（图或封闭）在一个 SQLite 写事务中完成全部校验、复制与版本指针切换，
    失败整体回滚；
  - 每次查询在开始时读取一次 `current_version` 并固定，**整次请求只用同一版本**，
    导入进行中或刚完成都不会看到半个版本；
  - 历史版本在库中保留，可按版本号追溯。

---

## 7. 项目结构

```
app/
  config.py        # 环境变量配置
  timeutil.py      # ISO 8601 <-> 纪元秒（UTC）
  schemas.py       # 导入/查询负载校验（含停靠点 stops）
  db.py            # SQLite 版本化与原子发布
  routing.py       # 时间依赖最短路 + 字典序选路引擎（含按顺序停靠）
  main.py          # FastAPI 路由
  sample_data.py   # 内置示例数据
  cli.py           # init-db / import-* / serve
data/              # 示例 JSON 与本地数据库
scripts/smoke_test.py
tests/             # 标准库 unittest 测试（63 个用例）
Dockerfile
docker-compose.yml
docker-entrypoint.sh
```

### 运行测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖半开区间边界、节点等候、封闭穿越判定、方向、避台阶、字典序平局、
按顺序停靠（窗口等待、顺序约束、全程字典序、起点/终点即站点）、
无路线、原子发布回滚与版本快照等场景。
