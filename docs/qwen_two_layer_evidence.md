# Qwen 活动双层证据运行说明

## 数据边界

- `event_occurrences` 是正式经营事实层：只接受官方、政府或主办方权威页面，原有人工记录优先。
- `event_candidates` 保留搜索候选；进入经营参考的 `corroborated` 状态需至少 2 个独立域名的实际页面分别支持同一活动日期、至少 1 个页面支持上海场馆，且至少 1 个来源是专业媒体或权威出版方。城市尚未确认的候选只保留线索。
- 搜索模型的综合回答、搜索摘要数量、同一网站的多个页面、论坛、自媒体和高度相同的转载内容都不能单独构成交叉证据。
- 多源候选只进入经营快照和预警参考，统一显示“多源证实，待官方核验”，绝不自动写入正式活动表。
- 官方取消、延期、改期或场馆变更优先；匹配到权威冲突后，候选参考会被抑制。

## 搜索频率与范围

- 全量发现保持原定周期，搜索未来 90 天；不增加系统定时器。
- 每次全量运行最多从近期高影响候选中选择 5 个做二次核验，每个活动只生成 1 条动态查询。
- 二次核验查询由活动名称、日期、场馆自动生成，不写死电竞或某个赛事名称。
- 每条候选最多读取 8 个公开 HTTPS 页面；证据 14 天过期，后续运行需要重新核验。

## 单候选验收

先执行 dry-run，只查看页面证据，不写数据库：

```bash
set -a; source ~/.hotel_agent/db.env; source ~/.hotel_agent/qwen-search.env; set +a
.venv/bin/python scripts/verify_qwen_candidate_sources.py --candidate-id <ID>
```

确认 `eligible=true` 后再增量写入候选层：

```bash
.venv/bin/python scripts/verify_qwen_candidate_sources.py --candidate-id <ID> --apply
```

## 回滚

1. 停止本轮正在执行的活动刷新任务，保留 timer 配置。
2. 从部署备份恢复本次覆盖的代码和配置。
3. 将本次变为 `corroborated` 的候选恢复到部署前状态，再删除新增索引和字段，恢复候选表的原约束。
4. 比对 `event_occurrences` 的部署前后指纹；该表不应因候选重评发生变化。

回滚不需要恢复或变更 Hermes 的 Alibaba 凭据，也不需要恢复 AnySearch。
