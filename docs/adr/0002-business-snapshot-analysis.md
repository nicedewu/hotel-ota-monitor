# ADR 0002：经营快照价格摘要与 Hermes 分析

## 状态

已确认，2026-06-29。

## 决策

- 实现位于 `hotel-ota-monitor` 的现有经营快照生成链路；`hotel_pms_agent` 继续只负责 PMS 查询和经营快照入库。
- 不修改 `run_scheduled_business_snapshot.sh` 的阶段、频率或锁，也不合并 `occupancy-watch`。
- PMS 继续通过 `pms-snapshot -> db-write-own-status -> collect_and_persist_today_summary()` 复用现有查询、签名和数据库逻辑。
- 价格摘要优先读取最新 `price_decisions.run_id`，然后使用现有 OTA 比价和最新自家 OTA 数据；最多展示 5 条。
- 飞书只展示经营概况、房型库存摘要、竞品价格摘要、规则建议和 Hermes 分析，不展示完整价格明细。
- Hermes 使用已安装 CLI 的一次性模式，忽略规则和记忆，只启用无本地读写能力的 `vision` 工具集，60 秒超时；失败时使用规则建议。
- Hermes 输入仅包含筛选后的结构化 PMS 指标和价格摘要，不包含路径、凭据、原始响应或完整 JSON。

## 影响

- 经营快照原有采集、抓价、入库、调价建议和通知顺序不变。
- 价格或 Hermes 读取失败不会阻止基础经营快照和规则建议生成。
- 现有 `notify_feishu.py` 继续发送 `business_snapshot_report.json.feishu_message`，无需新增 webhook 实现。
