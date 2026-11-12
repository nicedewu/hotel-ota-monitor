# ADR 0003：以自有酒店为多酒店任务边界

## 状态

已确认，2026-07-01。

## 决策

- 所有新增流程以 `own_hotel_key` 为主线，并只读取该酒店绑定的竞品和推送路由。
- 数据库配置优先，`hotels.json` 保留为数据库不可用时的兼容回退。
- 继续复用 `exhibition_events` 和 `agent_runs`；通过 `events`、`job_runs` 兼容视图提供新命名，避免重复事实源。
- 保留既有单店命令和 `output/latest/`，多酒店历史结果写入 `output/hotel_sets/<own_hotel_key>/<date>/`。
- 新多酒店 timer 只生成部署模板，不自动安装或启用，既有经营快照 timer 保持不变。

## 影响

- 新增酒店主要通过数据库配置；PMS 账号映射或 webhook 环境变量缺失时只跳过对应阶段并记录警告。
- `ota_room_prices` 和 `price_decisions` 同时保留旧字段与新字段，旧查询继续可用。
