# IoT设备数据接入与协议适配平台

面向IoT设备注册、多协议适配、消息路由与时序数据归集的设备数据接入平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 设备注册 | `device_registry` | 设备档案 | 设备编号、设备类型、协议类型 |
| 协议配置 | `protocol_cfg` | 协议配置 | 配置编号、协议名称、传输方式 |
| 报文解码 | `msg_decode` | 解码规则 | 规则编号、协议类型、报文标识 |
| 消息路由 | `msg_router` | 路由规则 | 规则编号、匹配Topic、目标Topic |
| 时序测点 | `ts_point` | 测点定义 | 测点编号、设备编号、测点名称 |
| 时序存储 | `ts_store` | 存储策略 | 策略编号、测点编号、存储引擎 |
| 降采样 | `downsample` | 降采样任务 | 任务编号、源测点、目标粒度 |
| 告警触发 | `alarm_trigger` | 告警规则 | 规则编号、关联测点、告警条件 |
| 固件升级 | `firmware_ota` | 升级任务 | 任务编号、目标设备、固件版本 |
| 网关管理 | `gateway_mgmt` | 网关节点 | 网关编号、网关地址、挂载设备数 |
| 设备影子 | `shadow_dev` | 影子记录 | 记录编号、设备编号、期望状态 |
| 数据断点 | `data_gap` | 断点记录 | 记录编号、设备编号、测点编号 |
| 批量导出 | `batch_export` | 导出任务 | 任务编号、设备范围、测点列表 |
| 规则引擎 | `rule_engine` | 联动规则 | 规则编号、触发条件、执行动作 |
| 边缘缓存 | `edge_cache` | 缓存配置 | 配置编号、网关编号、缓存大小 |
| 标签绑定 | `tag_binding` | 标签关系 | 关系编号、设备编号、标签键 |
| 数据流管道 | `data_stream` | 流管道 | 管道编号、输入源、处理算子链 |
| 聚合分发 | `aggregate_sink` | 分发任务 | 任务编号、数据窗口、聚合函数 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA

## 冷热分层与跨层导出（ts_store × batch_export）

容量、分层日期区间、导出预算与冷段生命周期共用 `backend/app/services/tiered_plan.py`
中的同一套策略，避免“页面容量一套数、导出计划另一套数”。

容量口径：`日字节 = 每日点数 × 每点字节 / 压缩比`；
`热层实际天数 = min(热层天数, 总保留天数)`（热层超长时只占实际留存天数），
`冷层天数 = max(总保留 - 热层实际, 0)`，因此热层/冷层日期不重叠，且
`热层字节 + 冷层字节 = 总保留字节`。例：每天 100 点、每点 32 字节、无压缩、
保留 5 天、热层 30 天 → 热层 16000、冷层 0。

页面：

- 「时序存储」页顶部可做容量试算，并可直接「提交跨层导出」；每行策略也有
  「跨层导出」操作（`data-testid="action-<id>-跨层导出"`），回填策略后生成
  带每个日期实际读取区间的「分层读取计划」。
- 「批量导出」的任务编号或「导出详情」进入 `/batch_export/:id`，逐段领取 /
  读取 / 确认 / 继续完成冷段；保留收缩触发冷层清理后，冷段区分为
  **等待读取 / 不可读（页面标记“已被释放”）/ 已交付**。确认前失去有效读取
  依据的日期不计费；已发出的片段幂等不重发；费用只按实际可取且确认交付的范围。

主要接口（同时提供 query / JSON body / 中文参数名与多组路径别名）：

- `GET|POST /api/ts_store/capacity`（及 `/estimate`、`/estimate_storage`）
- `POST /api/tiered-exports`、`GET /api/tiered-exports/{ref}`
- `POST /api/tiered-exports/{ref}/claim`
- `POST /api/tiered-exports/{ref}/segments/{seg}/read|confirm|complete`
- `POST /api/tiered-exports/{ref}/retention|cleanup`
- `GET /api/batch_export/{ref}/detail`、`GET|POST /api/batch_export/{ref}/read-plan`
- `PUT|PATCH /api/ts_store/{id}`（改保留天数会自动对账在途计划）
- Flow 10：`POST /api/cross/flow10/cross-tier-export` 等

