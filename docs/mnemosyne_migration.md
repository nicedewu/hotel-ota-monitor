# TimescaleDB + Mnemosyne 第一阶段并行接入

## 当前架构

- TimescaleDB 保存 `ota_room_prices`、`own_daily_status`、`price_decisions`、`exhibition_events`、`agent_runs` 等事实数据。
- Hermes 保留 `MEMORY.md`、`USER.md`、`SOUL.md`，并存在 mem0 provider/plugin 代码。
- `price-decision` 直接读取 TimescaleDB，按确定性规则生成建议。

## 目标架构与分工

```text
飞书 / Hermes
  -> 意图分流
     -> 房态、入住率、库存：TimescaleDB
     -> OTA 价格、竞品、调价建议：TimescaleDB + output/latest
     -> 规则、偏好、长期经验：Mnemosyne
```

TimescaleDB 继续是事实仓库，保存完整经营事实和运行记录。Mnemosyne 仅保存筛选后的安全规则、酒店规则、长期偏好和有结论的典型案例；不保存全量房价流水、每小时抓价明细、普通日志、重复快照或无结论数据。

## 多酒店隔离

- 全局规则：`~/.hermes/mnemosyne/global/mnemosyne.db`
- 酒店规则：`~/.hermes/mnemosyne/hotels/<hotel_key>/mnemosyne.db`
- `own_demo_a`：`~/.hermes/mnemosyne/hotels/own_demo_a/mnemosyne.db`

`src/memory_provider.py` 使用独立 SQLite 文件隔离全局与各酒店记忆。调价读取顺序为全局规则、指定 `hotel_key` 规则、TimescaleDB 事实数据。召回失败返回空列表，不阻断现有调价逻辑。

## 第一阶段行为

- `MNEMOSYNE_ENABLED=false`、`MNEMOSYNE_WRITE_MODE=staging` 为默认值。
- 生产 Hermes 使用 `MNEMOSYNE_NO_EMBEDDINGS=1`，避免首次召回下载模型；当前中文规则已通过 SQLite FTS 召回验证。
- 写入请求先落到 `output/memory_staging/mnemosyne_pending.jsonl`。
- 旧记忆只导出到 `existing_memory_export.jsonl`，不修改源文件。
- TimescaleDB 只读抽取结果写入 `timescale_experience_candidates.jsonl`，不修改数据库。
- `price-decision` 只增加 `memory_context` 和 `memory_context_used`；记忆不会改变现有计算公式。
- 本阶段不删除 mem0/Qdrant，不启用新 timer，不真实推送飞书，不修改数据库 schema。

## 验证命令

```bash
cd /opt/hotel-ota-monitor
python3 -m compileall src main.py scripts
python3 -m pytest -q
bash -n scripts/*.sh

python3 scripts/export_existing_memory_for_mnemosyne.py --dry-run
source ~/.hotel_agent/db.env
python3 scripts/memory_sync_from_timescale.py --dry-run --days 30

python3 scripts/export_existing_memory_for_mnemosyne.py
python3 scripts/memory_sync_from_timescale.py --days 30
python3 scripts/verify_mnemosyne_memory.py
```

验证脚本只读取 Mnemosyne 或 staging，不调用飞书。

## 生产切换步骤

以下步骤属于下一阶段，每一步都需单独确认：

1. 确认新增依赖后，在项目虚拟环境安装 `mnemosyne-memory`；Hermes 原生 provider 另行评估 `mnemosyne-hermes`，不要在本阶段安装。
2. 备份 staging、旧 Hermes 记忆文件和 Mnemosyne SQLite 文件。
3. 使用 `deploy/env/mnemosyne.env.example` 创建仓库外环境配置，将 `MNEMOSYNE_ENABLED=true`，先保持 `MNEMOSYNE_WRITE_MODE=staging` 验证召回。
4. 分别验证 global 与 `own_demo_a` 数据库，确认不存在跨酒店召回。
5. 经过中文二次确认后，将 `MNEMOSYNE_WRITE_MODE=direct`；TimescaleDB 同步脚本还必须显式传入 `--confirm-write 确认写入Mnemosyne`。
6. 验证 Hermes provider 后再决定是否停止旧 provider；不得先删除 mem0/Qdrant 或旧 Markdown 记忆。

## 回滚

1. 设置 `MNEMOSYNE_ENABLED=false`、`MNEMOSYNE_WRITE_MODE=staging`。
2. 保留 `memory_context` 代码也不会影响调价；如需代码回滚，只撤销本阶段文件和 `src/price_decision.py` 的小改动。
3. 继续使用原 Hermes `MEMORY.md` / `USER.md` / `SOUL.md` 与旧 provider。
4. 不删除 Mnemosyne 数据文件；先保留审计证据，再单独确认清理。

## 风险

- Mnemosyne 未安装或 API 变化：provider 自动回退 staging，召回返回空列表。
- Hermes provider 配置未生效：项目适配层与 Hermes provider 分开验证，不直接修改 Hermes 配置。
- 重复记忆：第一阶段仅生成候选，正式导入前按内容和酒店作用域去重。
- 多酒店串店：每个 `hotel_key` 使用独立数据库文件，验证必须覆盖跨店负例。
- 飞书安全规则丢失：旧记忆并行保留，切换前验证 PMS 价格禁用、飞书只读和敏感信息禁止规则。

## 第二阶段：正式导入 staging JSONL 并切换 Hermes 默认记忆到 Mnemosyne

### 导入前备份

执行任何写入前创建 `~/backups/mnemosyne_switch_<timestamp>`，保存 Hermes 配置、`MEMORY.md`、`USER.md`、`SOUL.md`、已有 Mnemosyne 数据和 `output/memory_staging`。备份只读保留。

### staging 审计和清洗

`scripts/import_staging_to_mnemosyne.py` 逐行统计 JSON 有效性、作用域、酒店、记忆类型、敏感内容和重复项。导入前执行：

```bash
python3 scripts/import_staging_to_mnemosyne.py --dry-run
```

清洗规则：

- 拦截 API Key、token、cookie、webhook、密码、Authorization、Bearer、数据库连接串和私钥内容。
- 含服务器内部路径或代码路径的内容不导入。
- `scope=hotel` 必须有 `hotel_key`；`own_demo_a` 只写入自己的独立 SQLite 文件。
- 周复盘分析输出、原始事实快照和未获人工确认的反思记录全部跳过。
- 按规范化内容、作用域、酒店和类型去重；近似重复保留高置信度或较新记录。
- 普通日志、堆栈、命令流水、原始 PMS/OTA 流水、无结论快照和低价值短句不导入。

清洗结果写入 `mnemosyne_import_cleaned.jsonl`，导入报告写入 `import_report.json`，失败索引写入 `mnemosyne_import_failed.jsonl`；失败文件不复制敏感原文。

### 正式导入

默认没有 `--apply` 时绝不写入 Mnemosyne：

```bash
python3 scripts/import_staging_to_mnemosyne.py --apply
python3 scripts/import_staging_to_mnemosyne.py --dry-run --scope global
python3 scripts/import_staging_to_mnemosyne.py --dry-run --scope hotel --hotel-key own_demo_a --limit 50
```

全局记忆写入 `global/mnemosyne.db`，酒店记忆写入 `hotels/<hotel_key>/mnemosyne.db`。TimescaleDB 仍是事实仓库。

### Hermes provider 切换

安装并注册 provider 后执行：

```bash
~/.hermes/hermes-agent/venv/bin/mnemosyne-hermes install --hermes-home ~/.hermes
~/.local/bin/hermes config set memory.provider mnemosyne
~/.local/bin/hermes config set memory.memory_enabled false
~/.local/bin/hermes config set memory.user_profile_enabled false
~/.local/bin/hermes memory status
```

不得执行 `hermes tools disable memory`。旧 `MEMORY.md`、`USER.md`、`SOUL.md` 和 mem0/Qdrant 保留，不再作为默认主记忆。

### 验证与重启策略

```bash
python3 scripts/verify_mnemosyne_live.py
source ~/.hotel_agent/db.env
python3 main.py price-decision --date "$(date +%F)" --days 1
```

检测到的 gateway 为用户级 `hermes-gateway.service`。默认不重启；只有明确设置 `ALLOW_HERMES_RESTART=true` 后才执行：

```bash
systemctl --user restart hermes-gateway.service
```

未重启前，配置和 provider 已准备完成，但正在运行的 gateway 进程可能仍使用启动时配置。

### 周复盘边界

`weekly-review` 仅将已验证事实提交给专用 Hermes 分析器；分析失败时使用确定性规则降级。两种模式都不创建经验候选、不写入 Mnemosyne，也不影响实时价格决策。

### 第二阶段回滚

```bash
BACKUP=~/backups/mnemosyne_switch_<timestamp>
cp -a "$BACKUP/hermes/config.yaml" ~/.hermes/config.yaml
```

将 `~/.hotel_agent/mnemosyne.env` 调整为：

```env
MNEMOSYNE_ENABLED=false
MNEMOSYNE_WRITE_MODE=staging
MEMORY_PRIMARY_BACKEND=staging
MEMORY_AUXILIARY_BACKEND=none
```

不删除 Mnemosyne 数据，不删除旧 Markdown 记忆，不删除 mem0/Qdrant。只有需要让运行中 gateway 重新加载回滚配置时，才在确认后重启服务。
