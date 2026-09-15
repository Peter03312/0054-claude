# 播客剪辑授权检查 API

校园播客社团的纯后端服务：接收剪辑图 JSON，沿无环图用**有理数精确**反推每个输出
段的全部原始来源区间，并校验这些区间是否被目标受众的许可段并集覆盖。任一来源未
许可即拒绝发布，并返回起点最早的违规段作为授权证据。

技术栈：Python 3.12 · FastAPI · Pydantic v2 · SQLite（标准库 `sqlite3`）。

## 数据模型与规则

### 剪辑图（`POST /edits` 的请求体）

```json
{
  "title": "episode-12",
  "nodes": [ ... ],
  "consents": [ ... ]
}
```

`nodes` 是按 `type` 区分的节点数组，图中不得有环、不得有悬空引用：

| 类型 | 字段 | 语义 |
| --- | --- | --- |
| `source` | `duration`, `contributors` | 原始素材，声明时长（正整数 tick）与贡献者 |
| `trim` | `child`, `start`, `end` | 取子节点的**半开区间** `[start, end)`，输出重置为零 |
| `speed` | `child`, `p`, `q` | 速率 `p/q`：输出时刻 `t` 映射到输入 `t × p/q` |
| `concat` | `children` | 按数组顺序顺接 |
| `mix` | `children: [{node, offset}]` | 各子节点按偏移叠加；无人覆盖的区域是静音段 |

约束：

- 时长、端点、偏移都是**非负整数 tick**；`source.duration` 与 `speed` 的 `p`、`q`
  必须为正（非正速率返回 422）。tick 字段按**严格整数**校验：JSON 字符串
  （`"100"`）、浮点数（`3.0`、`1.5`）或布尔值一律 422，不会被悄悄当成整数保存。
- 所有区间均为**半开** `[start, end)`：`start` 计入、`end` 不计入；`start < end`。
- `trim` 越界（`end` 超过子节点时长）、`consents` 越界（超过源时长）、循环、悬空
  引用、重复节点 id 都会返回**可定位的 422**（见下文错误契约），且**整单不保存**。

### 同意清单（consents）

```json
{"source": "interview", "start": 60, "end": 540, "audiences": ["students"]}
```

每条记录给出某个来源的一段半开许可区间和允许受众。对目标受众而言，某来源的
**许可集 = 所有包含该受众的记录区间的并集**（相邻半开段会无缝合并）。

### 授权判定

分析（`POST /edits/{id}/analyses`，请求体 `{"output": 节点id, "audience": 受众}`）
沿无环图用 `fractions.Fraction` 精确反推输出时间轴上每一段消费了哪些来源的哪些
原区间：

- 每个输出段为每个来源给出仿射映射 `input = ratio × t_out + offset` 与所用原区间。
- 某来源在一段内**许可 ⟺ 所用原区间 ⊆ 该来源对目标受众的许可并集**。
- 仅当相邻段的**来源集合、仿射映射、授权结论**三者都相同时才合并。
- 任一来源未许可 → `decision = "rejected"`，并返回**起点最早的违规段**。违规段
  会沿仿射映射把未许可的原区间**反推回输出时间轴**，精确到实际未许可的输出区间
  ——段首已获许可的节目不会被标入，社团可直接按 `violation.out` 定位需要删改的
  片段；其中全部未许可来源（含贡献者与该区间内未许可的原区间）按来源 ID 排序。

### 响应中的有理数

精确有理数在 JSON 中编码为：分母为 1 时是整数（如 `90`），否则是字符串
（如 `"5/2"`）。`speed` 可能产生分数时长与分数偏移，全部精确保留。

### 授权证据报告（示例节选）

```json
{
  "decision": "rejected",
  "output_duration": 190,
  "segments": [
    {"out": {"start": 0, "end": 90}, "licensed": true,
     "sources": [{"source": "s", "maps": [{"ratio": "2/3", "offset": 10}],
                  "used": [{"start": 10, "end": 70}],
                  "licensed": true, "uncovered": []}]}
  ],
  "violation": {
    "out": {"start": 90, "end": 100},
    "unlicensed": [{"source": "s", "contributors": ["amy"],
                    "uncovered": [{"start": 0, "end": 10}]}]
  }
}
```

### 错误契约

- 字段级错误（非正速率、负 tick、空半开区间等）由 Pydantic 返回 422，
  `detail[].loc` 指向出错字段。
- 图级错误（循环、悬空引用、越界、重复 id、许可指向非 source 节点）同样返回
  422，`detail[].loc` 形如 `["body", "nodes", 1, "child"]` 或
  `["body", "consents", 0, "end"]`，可定位到具体字段。
- 任何 422 都意味着**整单不保存**：编辑与分析快照都不会落库。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/edits` | 提交剪辑单（先整单校验再保存），返回各节点精确时长 |
| `GET` | `/edits` / `/edits/{id}` | 列表 / 读取已存剪辑单 |
| `POST` | `/edits/{id}/analyses` | 指定唯一输出节点与目标受众，运行分析并保存快照 |
| `GET` | `/edits/{id}/analyses` / `.../{analysis_id}` | 分析快照列表 / 读取 |

## 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
python -m pytest            # 运行测试
```

数据库路径由环境变量 `APP_DB` 指定（默认 `./app.db`）。

## Docker Compose

```bash
docker compose up --build          # api 常驻；verify 运行测试并请求分析入口后退出
API_PORT=9000 docker compose up    # 用 API_PORT 覆盖宿主端口（默认 8000）
```

- `api` 是唯一常驻服务，数据存于命名卷 `app-data`。
- `verify` 是一次性服务：先在容器内跑 `pytest`，再把社团整集节目提交给
  `api` 的分析入口，确认全片对目标受众获许可（输出 `VERIFY OK`），否则以非零
  状态退出并打印违规证据。

## 目录结构

```
app/
  models.py   # 请求/响应契约（Pydantic v2，判别联合节点模型）
  engine.py   # 区间来源引擎：仿射映射、扫描线合并、许可并集覆盖判定
  store.py    # SQLite 持久化（编辑单 + 分析快照）
  main.py     # FastAPI 路由与 422 错误处理
tests/        # 嵌套与分数变换、混音重叠、半开端点、坏图、快照读写
scripts/verify.py
Dockerfile / docker-compose.yml
```
