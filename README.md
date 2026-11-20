# FIB Compressor

边缘路由器换型配套工具：把现有 IPv4 转发表压缩成**条目最少**的等效表，装进新设备的容量预算。

- 输入：最多 128 条前缀互异的 IPv4 CIDR 路由（每条指定一个下一跳或 `drop` 丢弃）、新设备条目上限 `capacity`、一个要演练失效的下一跳 `failed_nexthop`。
- 输出：按最长前缀匹配（LPM）与原表**逐地址等价**的最小转发表——可换用新前缀，动作全部取自原表；`drop`（丢弃）与未匹配严格区分。
- 故障演练：把新旧两张表中指向 `failed_nexthop` 的条目全部撤掉后，两表仍须逐地址等价。压缩结果天然满足这一约束。
- 超限时只回报最低所需条数（`status=capacity_exceeded`，`table=null`）。
- `POST /resolve` 先把**递归静态路由**（下一跳仍是 IPv4 地址、需要逐跳解析到直连网段）解析成可直接提交给 `/compress` 的转发表。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

`/docs`（Swagger UI）的 `POST /compress` 内置三个示例，下拉即可切换：

| 示例 | 说明 |
|---|---|
| `capacity_ok` | 容量足够：5 条压到 4 条，撤掉 NH2 后两表仍等价 |
| `capacity_exceeded` | 容量不够：只回报 `min_entries=4`，不下发转发表 |
| `failover_ladder` | 故障回落链：/8→NH1、/16→NH2、/24→NH3，演练失效 NH2 |

`POST /resolve` 内置两个示例：`loop_fallback`（遇环回退，默认演示）与 `straight_chain`（直链递归）。

命令行演示（服务启动后）：

```bash
./scripts/demo.sh                # 容量够/不够压缩 + 静态路由遇环回退 + 转交 /compress
```

## API

### `POST /compress`

```json
{
  "routes": [
    {"prefix": "10.0.0.0/8", "action": "NH1"},
    {"prefix": "10.1.0.0/16", "action": "NH2"},
    {"prefix": "10.2.0.0/16", "action": "NH1"},
    {"prefix": "10.3.0.0/16", "action": "drop"},
    {"prefix": "192.168.0.0/16", "action": "NH2"}
  ],
  "capacity": 4,
  "failed_nexthop": "NH2"
}
```

容量足够时返回最小等效表（`10.2.0.0/16` 与 `10.0.0.0/8` 同为 NH1，被合并吸收）：

```json
{
  "status": "ok", "fits": true, "min_entries": 4, "verified": true,
  "table": [
    {"prefix": "10.0.0.0/8", "action": "NH1"},
    {"prefix": "10.1.0.0/16", "action": "NH2"},
    {"prefix": "10.3.0.0/16", "action": "drop"},
    {"prefix": "192.168.0.0/16", "action": "NH2"}
  ]
}
```

容量不够（如 `capacity=3`）时只报最低所需条数：

```json
{"status": "capacity_exceeded", "fits": false, "min_entries": 4, "table": null}
```

约束与约定：

- `routes` 最多 128 条，前缀互异；非规范写法（如 `10.1.2.3/8`）会归一化为网络地址。
- `action` 为下一跳名称，或保留字 `drop`；不匹配任何条目的地址是“未匹配”，与 `drop` 不同。
- 响应前服务端会对结果做逐区间等价复核（正常 + 故障两种场景），`verified=true` 才返回。

### `POST /verify`

独立校验两张表是否在正常与故障两种场景下逐地址等价，返回首个失配点。

### `POST /resolve`

把递归静态路由解析成可交给 `/compress` 的转发表。一次提交：

- `connected`：IPv4 直连网段及其出接口（接口名 1–32 字符），前缀互异，至少 1 条；
- `static_routes`：最多 128 条静态路由，下一跳为 IPv4 地址，前缀互异。

解析规则（每条路由从**自身下一跳**开始）：

1. 网关落在某个直连网段内 → 取**最长匹配**的直连网段，递归结束，出接口即该网段接口；
2. 否则按前缀**从长到短**尝试所有覆盖该网关的静态路由，沿依赖链继续解析；
3. 会重复经过同一路由的分支（环路）直接跳过，选**第一条**能到达直连网段的依赖链；
4. 环路/死路不影响其余路由——全量输入照常处理，不穷举所有可能的链。

```json
{
  "connected": [
    {"prefix": "192.168.10.0/24", "interface": "eth1"},
    {"prefix": "192.168.10.128/25", "interface": "eth2"},
    {"prefix": "10.0.0.0/8", "interface": "eth0"}
  ],
  "static_routes": [
    {"prefix": "172.16.1.0/24", "next_hop": "172.16.2.1"},
    {"prefix": "172.16.2.0/24", "next_hop": "172.16.1.1"},
    {"prefix": "172.16.0.0/16", "next_hop": "10.9.9.9"},
    {"prefix": "172.18.0.0/16", "next_hop": "192.168.10.200"},
    {"prefix": "172.19.0.0/16", "next_hop": "203.0.113.7"}
  ]
}
```

前两条 `/24` 互为下一跳构成环路；解析跳过环分支，经覆盖两者的 `172.16.0.0/16` 回退到直连 `10.0.0.0/8`。返回（节选）：

```json
{
  "status": "partial",
  "resolved_count": 4,
  "unresolved_count": 1,
  "resolved": [
    {
      "prefix": "172.16.1.0/24",
      "chain": [
        {"prefix": "172.16.1.0/24", "next_hop": "172.16.2.1"},
        {"prefix": "172.16.2.0/24", "next_hop": "172.16.1.1"},
        {"prefix": "172.16.0.0/16", "next_hop": "10.9.9.9"}
      ],
      "interface": "eth0",
      "final_gateway": "10.9.9.9",
      "connected_prefix": "10.0.0.0/8",
      "nexthop_id": "eth0:10.9.9.9",
      "route": {"prefix": "172.16.1.0/24", "action": "eth0:10.9.9.9"}
    }
  ],
  "unresolved": [
    {"prefix": "172.19.0.0/16", "next_hop": "203.0.113.7", "reason": "no_route"}
  ],
  "routes": [
    {"prefix": "172.16.1.0/24", "action": "eth0:10.9.9.9"}
  ]
}
```

字段与约定：

- `chain`：从路由**自身前缀**出发的依赖链，逐跳记录所用前缀及其下一跳；`final_gateway` 是第一个落在直连网段内的网关（递归终点）。
- `nexthop_id` = `出接口:最终网关`，作为下一跳标识；`routes` 里的每个 `action` 就是它，可直接原样放进 `/compress` 的 `routes`。
- `unresolved` 给出原因：`loop`（处于环路或只能走进环路）、`no_route`（某跳网关既不匹配直连网段也无静态路由覆盖）。
- 全量可解析时 `status="ok"`，否则 `"partial"`；输入前缀在两份列表内各自互异（两表之间允许重叠），非规范写法归一化为网络地址。

### `GET /`

服务信息。

## 算法（为什么是最少条目）

每个目的地址带一个二元需求 `(正常结果, 撤掉故障下一跳后的结果)`，由原表推出；两个函数在所有前缀边界切出的基本区间上都是常数（≤ 2n+1 段）。

1. 把地址空间建成“常数需求”二分前缀 trie（叶 = 极大常数需求 dyadic 区间）。
2. 在 trie 上做精确动态规划：状态是“上方最长匹配条目动作 + 上方最长非故障条目动作”的继承对（可达状态只有 `2k+2` 种，k 为非故障动作数）；叶节点用闭式代价（含“正常层=F、回落层=b”的双层结构，无法从祖先继承回落时最多需 3 条）；内部节点在“不放置 / 放置故障下一跳 / 放置某非故障动作”三种选择中取最优。
3. 回溯选择得到最小条目表。DP 是精确的，返回的条目数是可证明的最小值。

复杂度：`O(节点数 × 动作数)`，128 条路由实测 < 0.5s。

## 本地开发

```bash
pip install -r requirements-dev.txt
python -m pytest            # 76 项测试：压缩核心、静态路由解析、API
uvicorn app.main:app --reload
```

## 目录结构

```
app/compressor.py   # 压缩核心：需求区间 → 常数 trie → 双场景 DP → 回溯
app/resolver.py     # 递归静态路由解析：可达性标记 → 环剪枝 DFS → /compress 条目
app/main.py         # FastAPI：/compress、/verify、/resolve、/docs 示例
tests/              # 单元、随机性质（含暴力对照）、压力、API 测试
scripts/demo.sh     # 压缩、遇环回退解析与转交 /compress 的 curl 演示
Dockerfile / docker-compose.yml
```

## 静态路由解析算法（为什么遇环不会爆）

把每条静态前缀看成图节点：网关在直连网段内得到一条“终点边”，否则对每个覆盖该网关的静态前缀按前缀长度从长到短连边。

1. **可达性标记**：从所有终点边反向洪泛，能到达终点的节点才可解析；不可达节点（含一切环路成员）直接归入 `unresolved`，不做任何路径枚举。
2. **依赖链恢复**：DFS 沿 LPM 顺序选边，用当前路径集合剪枝——下一条边指向路径上已有节点即环路，跳过该分支；配合两个可靠备忘录（链复用需路径无交集、失败复用需上下文为子集），工作量保持多项式，绝不穷举全部链。
3. 不可达节点的原因由非可达子图的 SCC 判定：自环/多节点 SCC 及其可达上游为 `loop`，其余为 `no_route`。

128 条路由（含 128 节点纯环、带单一出口的大环、密集 DAG）实测均在毫秒级完成。
