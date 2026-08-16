# Research: 跨实例事件流与 SSE 断线续传（R-T-5 / A11）

- **Query**: 验证 prd R-T-5 与 A11「同一会话的事件流可从任一应用实例订阅到，实例重启不丢已落库事件」：Redis Pub/Sub vs Streams 选型、两套 ID 对齐、SSE 实现细节、写者互斥边界、现状差距清单
- **Scope**: mixed（仓库走读 + redis.io / WHATWG / nginx 官方文档 + 已安装依赖源码）
- **Date**: 2026-08-15
- **前置**: 建立在 `research/agentos-event-contract.md` 与 design.md 5.4–5.6 之上，不重复其内容

## 结论速览

| 问题 | 结论 |
|---|---|
| Q1 选型 | **Redis Streams**（每会话一个 stream）；**不用 consumer group**（竞争消费语义与全量扇出相反） |
| Q2 ID | SSE `id:` = DB `session_events.seq`；Redis 侧用 **XADD 显式 ID `{seq}-0`**，两套 ID 合并为一套 |
| Q3 SSE | uvicorn(spec 2.3) 下 starlette 主动监听断线并取消流；心跳 15s 注释帧必要；nginx 需 `X-Accel-Buffering: no`；现端点为 POST，原生 EventSource 不可用，`Last-Event-ID` 须客户端手动带或新增 GET 订阅端点 |
| Q4 互斥 | 写者互斥唯一源 = PG advisory lock；Redis 不做互斥，显式 ID 单调约束仅作防错窗 |
| Q5 差距 | StreamBus 需拆写侧/读侧；SSE 帧与响应头全面升级；断线语义须与中止运行解耦；config 新增约 6 个 `AGENTOS_EVENTS_*` 键 |

---

## 一、现状增量证据（事件契约清单之外的新发现）

### 1.1 断线不只丢事件，而是直接杀死运行中的 Agent

`runtime/agent.py:123-146`：`run()` 生产者-消费者模式中，SSE 消费端（HTTP 连接）一旦关闭，
生成器 `finally` 分支执行 `producer.cancel()`（:141-142）；`_produce` 的
`except asyncio.CancelledError`（:217-226）随即 `_mark_abnormal_end(..., "interrupted")`。
即**写者生命周期 = HTTP 请求生命周期**——这正是 design 5.5 所述差异的代码实体。
A11 要求「任一实例可订阅」，前提是写者与订阅者解耦：断线只能结束订阅，不能中止运行。

### 1.2 SSE 端点均为 POST 且零附加响应头

- `session/api.py:42-70`（`POST /sessions/messages`）与 `interaction/api.py:79-120`
  （`POST .../resolve`）：`StreamingResponse(event_stream(), media_type="text/event-stream; charset=utf-8")`，
  未设置任何缓存/代理头，无心跳，无 `id:`（`format_sse` 见 `runtime/agent.py:1710-1712`，仅 `data:` 行）。
- **POST 这一点直接决定 Q3 的 Last-Event-ID 答案**（见第四节）。

### 1.3 Redis 配置是「死配置」：三处已同步但零消费者

- `core/config.py:51-54` 声明 `redis_url` / `redis_password` / `disable_redis_events`；
  `.env:37-41` 与 `.env.example:34-38` 均有对应 `AGENTOS_REDIS_*` 键（三处同步已满足）。
- 但全仓 `grep "import redis|from redis"` 零命中——不存在任何 Redis 代码路径，
  `disable_redis_events` 是无代码读取的预留开关（实现 P6-5 时须激活或按清理规则处置）。

### 1.4 依赖版本（uv.lock 锁定）

| 包 | 版本 | 说明 |
|---|---|---|
| redis (redis-py) | 8.0.0 | `pyproject.toml:20` 直接依赖 `redis>=5.2.0`；含 `redis.asyncio` 异步客户端 |
| starlette | 1.3.0 | fastapi 0.136.3 传递 |
| uvicorn | >=0.49.0 | httptools/h11 两个 impl 均声明 ASGI `spec_version: "2.3"`（见 4.1） |
| sse-starlette | 3.4.5 | **传递依赖**（经 `mcp[cli]`，uv.lock:661/1121）；若采用须提升为直接依赖 |

### 1.5 Redis 连通性验证（未通过）

按 `.env` 现有 `AGENTOS_REDIS_URL`（scheme=redis，默认 db）用 redis-py PING：
`redis.exceptions.ConnectionError: Connection closed by server.`——TCP 可达但对端主动关闭。
无法区分是服务端状态还是本会话沙箱对非 HTTP 流量的拦截；未擅自安装/启动任何服务。
**Redis 版本与可用性：未验证**（Streams 需 Redis >= 5.0，实现前须确认）。

---

## 二、Q1 选型：Redis Pub/Sub vs Streams → 推荐 Streams

### 2.1 官方语义（redis.io 原文）

Pub/Sub（`redis.io/docs/latest/develop/pubsub/`，Delivery semantics 节）：
> "Redis' Pub/Sub exhibits **at-most-once** message delivery semantics ... If the subscriber is
> unable to handle the message (for example, due to an error or **a network disconnect**) the
> message is **forever lost**. If your application requires stronger delivery guarantees, you may
> want to learn about Redis Streams."

Streams（`redis.io/docs/latest/develop/data-types/streams/`）：
- 条目持久、原生 entry ID 格式 `<millisecondsTime>-<sequenceNumber>`；
- 任意起点续读：`XREAD` 的 streams 参数即「IDs indicate the last ID already seen」
  （redis-py 8.0.0 `commands/core.py:7658-7666`）；
- 扇出是官方设计目标之一："we want the streams to be able to **fan out** messages to multiple
  clients (the same way many `tail -f` processes can see what is added to a log)"；
- `MAXLEN`/`XTRIM`/`MINID` 裁剪成 capped stream（redis-py `xadd` 参数
  `maxlen/approximate/minid`，`core.py:6755-6768`）。

### 2.2 逐项对比（本场景三个硬指标）

| 指标 | Pub/Sub | Streams |
|---|---|---|
| 多实例订阅同一会话 | 可（天然广播） | 可（各读者独立游标 XREAD，即 tail -f 模型） |
| SSE 断线续传 | **不可**：无 ID 概念，断线期间消息 forever lost，重连只能全量回 DB | 原生：从 Last-Event-ID 对应 entry 续读 |
| 实例重启不丢已落库事件 | 事件本体靠 DB 保证，但「重连订阅↔查 DB」之间存在缝隙消息，需三步舞去重合并 | 先定游标再 XREAD，天然无缝；Redis 冷启动时回落 DB |

### 2.3 推荐与理由

**推荐 Redis Streams，每会话一个 stream key（如 `agentos:events:{session_id}`）**：

1. A11 的「不丢已落库」由 `session_events`（DB，design 5.4）保证，Redis 只是**扇出管道 +
   热区缓存**；Pub/Sub 连热区都没有，每次重连都得打 DB，且订阅建立与 DB 查询之间的缝隙
   消息需要去重合并；Streams 用一个 ID 游标同时解决「续传起点」与「缝隙」两个问题。
2. Streams 的 MAXLEN 给出明确内存上界；Pub/Sub 的积压靠 `client-output-buffer-limit pubsub`
   超限断连（订阅者被踢），与 SSE 长连接相性差（此条未在本次抓取页面逐字核对，标注：弱证据）。
3. redis-py 8.0.0 异步客户端对 XADD/XREAD/XRANGE 全覆盖（1.4 节），零新增依赖。

### 2.4 consumer group：明确不用

官方定义（streams 文档原文）：
> "groups of consumers can only see **a subset of the messages** arriving in a single stream ...
> each consumer will just get **different messages** to process. This is basically what Kafka (TM)
> does with consumer groups." / "Redis consumer groups are a server-side **load balancing** system
> of messages from a given stream to N different consumers."

即 XREADGROUP 是**竞争消费**：组内每条消息只投给一个消费者——与本场景
「多个实例订阅同一会话、每个订阅者都要收到全量」**恰好相反**。
若强行为每个 SSE 连接建独立 group，还要背上 XACK / PEL / XGROUP DESTROY 的生命周期管理。
扇出场景的正确用法就是普通 `XREAD BLOCK`（或 XRANGE 补页 + XREAD 接活流），每个读者自持游标。

---

## 三、Q2 ID 对齐：用 XADD 显式 ID 把两套 ID 合并成一套

### 3.1 前提（design 已定，不重新发明）

design 5.6 缺口 #2/#3 已定：日志事件 `(session_id, seq, occurred_at)`，seq 由 DB 单调分配；
SSE 帧 `id:` = 日志 seq。因此 **SSE 的 `id:` 必用 DB seq**，剩下的问题只是 Redis entry ID
如何与之对齐。

### 3.2 方案：写者转发时 `XADD key {seq}-0`

Redis 官方明示支持显式 ID 对接外部系统序号（streams 文档原文）：
> "If for some reason the user needs incremental IDs that are **not related to time but are
> actually associated to another external system ID** ... the XADD command can take an explicit
> ID instead of the * wildcard"

约束与收益：
- 同一 stream 内显式 ID 必须单调递增，否则报错
  `ERR The ID specified in XADD is equal or smaller than the target stream top item`
  （官方教程条目："XADD rejects IDs that are not monotonically increasing"）。
  本场景恰好满足：每会话单写者（advisory lock）+ seq 每会话单调 ⇒ 恒合法；
  副作用是**免费幂等**——重复转发同一 seq 会被 Redis 拒绝，不产生重复帧。
- entry ID 与 DB seq 之间是纯算术关系（`{seq}-0` ⇄ seq），**无需任何映射表**。

### 3.3 续传解析流程（服务端收到重连请求，Last-Event-ID = N）

1. 解析 N 为整数（非法值视同无 Last-Event-ID：按第七节建议从 DB 全量/快照起点重放）；
2. 先读 Redis 热区：`XRANGE key ({N}-0 + COUNT batch`（左开区间）或
   `XREAD streams={key: "{N}-0"}`；
3. **裁剪检测**（因 entry ID = `seq-0`，判断是纯整数比较）：
   - 返回非空且首条 ID 的 ms 部分 == N+1 → 无洞，推帧后继续 `XREAD BLOCK` 接活流；
   - 首条 > N+1，或 stream 不存在/为空（Redis 重启丢热数据、key 被 TTL 回收、MAXLEN 已裁）
     → **回落 DB**：`SELECT ... FROM session_events WHERE session_id=? AND seq>N ORDER BY seq`
     分批推送至 DB 尾部 seq=M；
4. 补完 DB 历史后从 `{M}-0` 起 `XREAD BLOCK` 接活流；若 Redis 完全不可用，
   可降级为纯 DB 轮询（损实时性，不损正确性）；
5. 每帧携带 `id: {seq}`；客户端始终以最后收到的 seq 作为下次重连的 Last-Event-ID。

「先 DB 后 Redis」的写序（见第五节）保证：**Redis 里出现的事件必已落库**，
故客户端持有的任何合法 Last-Event-ID 在 DB 中必能找到 ⇒ 续传起点永不悬空。

### 3.4 被否掉的备选：auto ID + payload 内带 seq

XADD 用 `*` 自动 ID、把 seq 放进字段：续传时无法用 seq 定位 entry，只能
「从 `$` 订阅新流 + DB 补历史 + 按 payload seq 去重合并」，等于把 Pub/Sub 的三步舞搬回来；
或者维护 seq→entry ID 的辅助映射（多一份状态、多一处不一致面）。均劣于显式 ID，不采用。

---

## 四、Q3 SSE 实现细节（证据：已安装依赖源码 + WHATWG + nginx 官方）

### 4.1 断线检测：本项目部署形态下由 starlette 主动监听，无需轮询

- uvicorn 两个 HTTP 实现均声明 ASGI `spec_version: "2.3"`
  （`.venv/.../uvicorn/protocols/http/httptools_impl.py:227`、`h11_impl.py:206`）；
- starlette 1.3.0 `StreamingResponse.__call__`（`.venv/.../starlette/responses.py:257-280`）：
  spec < 2.4 时开 task group **并发运行 `listen_for_disconnect(receive)`**（:242-246），
  收到 `http.disconnect` 即取消整个 scope → 应用的事件生成器被取消/关闭。
  ⇒ **断线检测主动且及时，不依赖下一次写**；`run()` 的 finally（agent.py:139-146）正是被它触发。
- `request.is_disconnected()`（`.venv/.../starlette/requests.py:318-330`）：用已取消的
  CancelScope 对 receive 做零阻塞单次 poll，单次代价极小；但需调用方主动轮询、每次最多消费
  一条 ASGI 消息。在 StreamingResponse 场景**不需要**它；适用场景是非流式长任务的中途探测。
- spec >= 2.4 的服务器走另一分支（responses.py:267-271）：断线要等**下一次 send 抛 OSError**
  才暴露——这是心跳的第二个作用：保证最迟一个心跳间隔内发现断线（换 ASGI 服务器时仍成立）。

### 4.2 心跳：必要，15 秒注释帧

- WHATWG HTML 标准 §9.2.7 Authoring notes 原文：
  > "Legacy proxy servers are known to, in certain cases, drop HTTP connections after a short
  > timeout. To protect against such proxy servers, authors can include **a comment line (one
  > starting with a ':' character) every 15 seconds or so**."
- nginx `proxy_read_timeout` 默认 **60s**："set only between two successive read operations ...
  If the proxied server does not transmit anything within this time, the connection is closed"
  （ngx_http_proxy_module 官方文档）⇒ 无心跳的空闲流会被 nginx 掐断。
- 注释行被客户端按规范忽略（WHATWG §9.2.6："If the line starts with a U+003A COLON character
  (:) — Ignore the line"），不影响客户端的 last event ID 状态。
- 参考实现 sse-starlette 3.4.5（`.venv/.../sse_starlette/sse.py`）：`DEFAULT_PING_INTERVAL = 15`
  （:259），ping 帧为注释行 `: ping - <UTC时间>`（:445-448），与规范建议一致。

### 4.3 反向代理缓冲：标准头配置

- nginx 官方：`proxy_buffering` 默认 **on**；可由上游响应头 **`X-Accel-Buffering: no`**
  按响应关闭（"Buffering can also be enabled or disabled by passing 'yes' or 'no' in the
  'X-Accel-Buffering' response header field"）；注意该能力可被 `proxy_ignore_headers` 禁用，
  部署文档须写明 nginx 侧兜底 `proxy_buffering off;`。
- sse-starlette 默认头组合（sse.py:305-313）：`Cache-Control: no-store`（setdefault）、
  `Connection: keep-alive`、`X-Accel-Buffering: no`——可直接抄作业。
- 现状：两个端点只设了 media_type，**上述头一个都没有**
  （session/api.py:70、interaction/api.py:120）。

### 4.4 Last-Event-ID：本项目现有端点形态下不会自动出现

- WHATWG §9.2.4 原文："The `Last-Event-ID` HTTP request header reports an EventSource object's
  last event ID string to the server **when the user agent is to reestablish the connection**."
  ——仅**原生 EventSource 的自动重连**才自动携带；`id` 字段值更新客户端缓冲
  （§9.2.6，值含 NULL 则忽略；空 `id` 会清空缓冲，导致后续重连不带该头）。
- 原生 EventSource 构造器仅 `new EventSource(url[, {withCredentials}])`——**GET、无 body、
  无自定义头**。而本项目两个 SSE 端点均为 **POST**（1.2 节）⇒ 原生 EventSource 根本不可用，
  前端必然是 fetch/ReadableStream 手写解析 ⇒ **Last-Event-ID 必须客户端手动在重连请求上带**
  （fetch 可自由设头，此路可通，但自动重连、retry 退避也须自实现）。
- 结构性建议（供 design 采纳，见第七节）：把「发消息」与「订阅」拆开——
  `POST /sessions/{id}/messages` 触发运行，`GET /sessions/{id}/events` 专职订阅；
  GET 端点让原生 EventSource（自动重连 + 自动 Last-Event-ID）可用，且**任一实例都能 serve
  订阅**，这本身就是 A11「从任一应用实例订阅」的自然接口形状。

---

## 五、Q4 写者唯一性与扇出的职责边界

结论：**互斥唯一源 = PostgreSQL advisory lock（design 5.5 已定）；Redis 侧不做写者互斥。**

| 层 | 职责 | 不承担 |
|---|---|---|
| PostgreSQL | 真源（session_events）、**单写者互斥**（advisory lock）、seq 分配、断点补读 | 实时扇出 |
| Redis Streams | 扇出管道、热区续传缓存（MAXLEN 有界） | 互斥、真源、跨重启保证 |

理由：
1. **单一互斥源原则**：再加一把 Redis 锁（SETNX/Redlock 类）会出现「两把锁判定不一致」的
   分裂脑窗口，且 Redis 锁的过期/续期语义弱于「持连接即持锁」的 PG advisory lock；
   多实例下判定「谁是写者」的依据必须唯一。
2. **写序固定为「先 DB append（拿 seq、commit）→ 后 XADD `{seq}-0`」**：
   - 保证 Redis 中只有已落库事件（A11 反向也成立：订阅者拿到的 id 必能回 DB 定位）；
   - XADD 失败（Redis 抖动）只损实时性不损数据：订阅者在下一条发现 seq 跳号 → 走 DB 补洞；
     写者亦可重试补发（显式 ID 幂等，重复无害）。
3. **显式 ID 的单调约束是免费的最后防线而非互斥机制**：若 advisory lock 失效边界出现双写者，
   落后者的 XADD 会因 ID ≤ top 被 Redis 拒绝，不会污染流；DB 侧仍需
   `(session_id, seq)` 唯一约束兜底（属 P4 建表范畴）。
4. **读者侧完全无锁**：任意实例任意数量 XREAD/XRANGE + DB 回落，与写者所在实例解耦——
   这正是「任一实例均可订阅」的实现语义。

---

## 六、Q5 现状差距清单

### 6.1 `core/event_bus.py`（StreamBus 全文 50 行，全部语义都要动）

| 现状 | 位置 | 要改成 |
|---|---|---|
| 进程内 `asyncio.Queue(maxsize=1000)`，每请求新建（`runtime/agent.py:121`） | event_bus.py:18-19 | 写侧发布器与读侧订阅器角色分离；发布器进程级共享，按会话定位 stream key |
| `sequence` 请求内自增注入 | event_bus.py:31-32 | seq 改由 DB append 分配（design 5.6 #2，P4 先行），bus 不再造号 |
| `close()` 放内存哨兵 `_DONE` | event_bus.py:37-42 | 跨实例订阅者看不到别进程的内存哨兵：终结语义改为**显式终结事件**（design 5.6 #5「turn/end 必发」） |
| close 后 emit 静默丢弃 | event_bus.py:29-30 | 写侧以 DB append 成功为准，不存在「掉进已关闭队列」一说 |
| 队列满自然背压 | event_bus.py:16 注释 | 改为 MAXLEN 裁剪 + DB 兜底，另定写侧对 Redis 失败的重试策略 |

新服务形状按 design 5.7：持有 `async_sessionmaker` 与进程级 redis 异步客户端（连接池），
不持有 `AsyncSession`；18 处 `bus.emit` 调用点（见 agentos-event-contract.md 第二节）签名
可保持，替换的是 bus 背后的实现。

### 6.2 SSE 返回处（session/api.py + interaction/api.py + format_sse）

| # | 改动 | 现状证据 |
|---|---|---|
| 1 | 帧加 `id: {seq}` 行（可选加 `event:`）；补显式终结帧 | `runtime/agent.py:1710-1712` 仅 `data:` |
| 2 | 响应头补 `Cache-Control: no-store`、`X-Accel-Buffering: no` | session/api.py:70、interaction/api.py:120 仅 media_type |
| 3 | 心跳 15s 注释帧（事件等待加 timeout 或独立心跳任务） | 全仓无心跳 |
| 4 | **断线语义解耦**：连接关闭只结束订阅，不得再触发 `_mark_abnormal_end("interrupted")` | `runtime/agent.py:139-146, 217-226` |
| 5 | 新增 `GET /sessions/{id}/events` 订阅端点：解析 `Last-Event-ID` 请求头，走 3.3 续传流程 | 现两端点均 POST，无重连入口 |
| 6 | 可选：直接采用 sse-starlette `EventSourceResponse`（头/心跳/断线监听齐备），须提升为 pyproject 直接依赖 | 现为 mcp[cli] 传递依赖 |

### 6.3 `core/config.py`（.env / .env.example / Settings 三处同步，extra="forbid" 强制）

已有（激活即可，勿重复新增）：`redis_url` / `redis_password` / `disable_redis_events`
（config.py:51-54）。`disable_redis_events` 当前无任何读取方——实现时要么成为正式开关
（建议被 `events_transport` 吸收），要么按 CLAUDE.md 功能清理规则删除。

建议新增（均带 `AGENTOS_` 前缀，命名沿用现有 snake_case 风格）：

| Settings 字段 | 建议默认 | 用途 |
|---|---|---|
| `events_stream_maxlen: int` | 4096 | 每会话 stream 的 MAXLEN（approximate 裁剪） |
| `events_stream_ttl_seconds: int` | 86400 | 会话 stream key 空闲 TTL 回收 |
| `events_heartbeat_seconds: float` | 15.0 | SSE 心跳注释帧间隔（与 WHATWG 建议一致） |
| `events_read_block_ms: int` | 5000 | XREAD BLOCK 毫秒（心跳节拍的实现细节） |
| `events_backfill_batch_size: int` | 500 | DB 补历史每批条数 |
| `events_transport: str`（"redis"/"memory"） | "memory" | 单实例/测试降级开关，**吸收 `disable_redis_events`** |

注意：`.env` 写本地实际值、`.env.example` 只写安全示例值；新增键若三处缺任一处，
`extra="forbid"`（config.py:20）会在启动时报错，是同步的机器强制。

### 6.4 顺序依赖与测试

- **P6-5 依赖 P4-2/P4-4**：没有 `session_events` 表和 DB seq，`id:` 无值可填、
  Redis 无显式 ID 可用。implement.md:157-158（P4-2 含「SSE 帧 `id:` 与 `Last-Event-ID` 续传」）
  与 :206（P6-5「Redis 事件总线跨实例 + SSE `Last-Event-ID` 续传打通」）**职责重叠**，
  建议标明分界：P4-2 做「帧带 id + 单实例续传（DB 补读）」，P6-5 做「Redis 扇出 + 跨实例」。
- 测试（implement.md:211 `tests/events`）：按 design 十一节，会话日志 / advisory lock 须
  真实 PostgreSQL；A11 需真实 Redis + 两个 app 实例（同库同 Redis）验证
  「A 实例写、B 实例订阅、B 重启后携 Last-Event-ID 续传不丢不重」。

---

## 七、对 design.md / prd.md 的修正建议

### 7.1 design.md：新增一节「跨实例事件平面」（建议编号 5.8，紧随 5.7 之后）

现状 R-T-5 在 design 中只有 5.5 末尾一句「由 R-T-5（Redis 事件总线）承载」，无任何展开。
新增节至少应固化以下决策（本文件第二至五节为论证底稿）：

1. **拓扑**：写者（advisory lock）→ `session_events` append（DB 分配 seq）→
   `XADD agentos:events:{session_id} {seq}-0` → N 个订阅者（任一实例）XREAD；
   Redis 定位为**扇出管道 + 热区缓存，非真源**；
2. **不用 consumer group** 及理由（竞争消费 vs 全量扇出，见 2.4）；
3. **ID 对齐规则**：stream entry ID = `{seq}-0`，SSE `id:` = seq；续传解析流程（见 3.3）
   与「热区被裁剪→回落 DB」的判定方法；
4. **SSE 传输规范**：心跳 15s 注释帧、`Cache-Control: no-store` + `X-Accel-Buffering: no`、
   显式终结帧、**断线 ≠ 中止运行**（与 5.5 的「日志驱动接管」呼应，现状反例见 1.1）；
5. **端点形状**：订阅与发消息分离，新增 `GET /sessions/{id}/events`
   （原生 EventSource 可用、浏览器自动带 Last-Event-ID）；两个 POST 端点的既有 SSE 行为如何过渡；
6. **配置清单**（见 6.3）与降级路径（memory transport；Redis 不可用时纯 DB 轮询）。

另建议在 **5.5 末尾补一句**：「写者互斥唯一源是 PG advisory lock，Redis 侧不做互斥；
XADD 显式 ID 的单调拒写仅作双写防错窗」——否则实现者容易顺手加 Redis 锁，造成双互斥源。

### 7.2 prd.md：R-T-5 建议改写，A11 建议补一句

- **R-T-5（prd.md:213）现文**「事件总线跨实例（Redis），任一实例均可订阅同一会话的事件流」
  未界定 Redis 的角色，容易被读成「Redis 是事件真源」。建议改写为：
  「跨实例事件：PostgreSQL 事件日志为真源，Redis Streams 作扇出与热区续传；
  任一实例均可订阅同一会话的事件流；SSE 支持 `Last-Event-ID` 断点续传，
  热区被裁剪时回落 DB 补读」。
- **A11（prd.md:284-285）可保留**（两个可观察结果均可验收），建议**补一句续传验收**：
  「客户端携带 `Last-Event-ID` 重连（含跨实例重连）后，收到的事件序列相对日志
  无缺号、无重复」。否则 A11 测不到续传正确性，而 implement P6-5 明确要求
  「SSE `Last-Event-ID` 续传打通」，验收与任务将不闭环。
- implement.md P6-5 建议按 6.4 拆分（写侧转发 / 读侧订阅+回落 / SSE 帧升级+GET 端点 /
  断线语义解耦），并标注对 P4-2/P4-4 的依赖——一行任务实际隐藏了四个改造面。

---

## 引用来源

- redis.io（2026-08-15 抓取）：`/docs/latest/develop/pubsub/`（Delivery semantics）、
  `/docs/latest/develop/data-types/streams/`（Entry IDs / explicit IDs / fan out /
  consumer groups / Capped Streams）
- WHATWG HTML Standard §9.2 Server-sent events（Last Updated 2026-08-11）：
  §9.2.4 `Last-Event-ID` 头、§9.2.6 注释行与 `id` 字段处理、§9.2.7 心跳建议
- nginx `ngx_http_proxy_module` 官方文档：`proxy_buffering`（默认 on）/
  `X-Accel-Buffering` / `proxy_ignore_headers` / `proxy_read_timeout`（默认 60s）
- 已安装依赖源码（.venv，与 uv.lock 一致）：
  starlette 1.3.0 `responses.py:242-280`、`requests.py:318-330`；
  uvicorn `protocols/http/httptools_impl.py:227`、`h11_impl.py:206`；
  sse-starlette 3.4.5 `sse.py:259, 305-313, 445-448`；
  redis-py 8.0.0 `commands/core.py:6724-6768`（xadd）、`:7637-7666`（xread）

## Caveats / 未验证

1. **Redis 实例连通性与版本未验证**（1.5 节）：`AGENTOS_REDIS_URL` 可解析，
   但 PING 被对端断开（`ConnectionError: Connection closed by server.`），
   无法排除本会话沙箱对非 HTTP 流量的干扰；实现/联调前须在正常环境复验。
2. Pub/Sub 的 `client-output-buffer-limit pubsub` 断连行为未逐字核对官方页面
   （2.3 节已标注弱证据），不影响选型结论（主要依据是 at-most-once 与无续传 ID）。
3. design.md 在本调查期间存在并行编辑（前后两次读取行号漂移约 20 行），
   故本文对 design 的引用一律用**节号**；5.4–5.6 的正文内容已在调查末尾复核未变。
4. 浏览器端 fetch-SSE 客户端库（如 fetch-event-source 类）未调查——仓库无前端工程
   （prd「不在范围内」明确排除前端建设，本文只保证服务端契约可被消费）。
5. 分支中间态提醒（沿袭 agentos-event-contract.md）：`runtime/agent.py:32` 仍
   `from llm.client import LLMClient` 而该模块已删除，当前工作区不可直接运行；
   本文所有 runtime 行号基于 2026-08-15 工作区。
