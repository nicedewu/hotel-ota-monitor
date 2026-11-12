# Hotel OTA Monitor

这个项目用于 Windows 本机低频测试携程酒店详情页的房型和价格抓取。工具只打开正常网页，只监听网页自然加载产生的网络响应，并将候选 JSON、页面调试文件和 JSON / JSONL / Markdown 结果保存到本地。

## 边界

- 这是人工登录版工具，不自动登录携程。
- 不保存账号密码。
- 不读取真实 Chrome Profile。
- 不复用日常 Chrome 用户目录。
- 登录态只保存在 `profiles/ctrip_manual_profile`。
- `profiles/ctrip_manual_profile` 已加入 `.gitignore`，不得上传 GitHub，也不得同步到云服务器。
- 不绕过验证码、短信验证、滑块验证或安全验证。
- 不破解签名，不逆向加密接口，不伪造携程内部接口请求。
- 不使用代理池，不做高频请求。
- 只适合低频内部测试，不适合大规模商业化抓取。
- 禁止调用 PMS 的房价、价格、价格计划、门市价、渠道价等价格接口。
- PMS 后续如需接入，只允许读取房间总数、当前在住房数、今日可售、今日已售、今日预订、入住率、未来库存、订单状态等经营数据。
- OTA 房型价格对比中，自己酒店价格也必须来自携程/OTA 抓取结果，不能来自 PMS。
- PMS 的任何价格字段不得进入 `price_compare.json`、`hermes_payload.json` 的价格对比逻辑。

## 安装

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -U pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium
```

## 人工登录

开始前按下节说明创建本地 `hotels.json`，并填入要访问的携程酒店详情页。

默认使用独立 profile：`profiles/ctrip_manual_profile`。

```powershell
.venv\Scripts\python.exe main.py login --channel chrome
```

浏览器打开后，在页面里手动完成携程登录。确认页面能看到酒店价格后，回到终端按 Enter。程序会保存登录检查调试文件到 `output/debug/login_check_*`，并输出页面状态。

也可以指定配置里的某个酒店打开：

```powershell
.venv\Scripts\python.exe main.py login --channel chrome --hotel-key own_demo_a
```

如果 Chrome channel 不可用，改用 Playwright Chromium：

```powershell
.venv\Scripts\python.exe main.py login --channel chromium
```

## 酒店配置 hotels.json

将 `hotels.example.json` 复制为 `hotels.json`，填入自营与竞品酒店的配置，再运行登录或抓取命令。示例 URL 使用保留域名。`hotels.json` 已加入 `.gitignore`，可以保留为本机配置。

`role` 支持：

- `own`: 自己的酒店。
- `competitor`: 竞争酒店。
- `unknown`: 未分类酒店，不参与价格对比，但会保留在原始汇总里。

可以配置多个竞争酒店。`role` 缺失时默认 `unknown`，`output` 缺失时默认 `output/<key>`。

```json
{
  "hotels": [
    {
      "key": "hotel_key",
      "role": "own",
      "name": "酒店名称",
      "url": "携程酒店详情页URL",
      "output": "output/hotel_key"
    }
  ]
}
```

## 抓取

抓取单个配置酒店：

```powershell
.venv\Scripts\python.exe main.py scrape --channel chrome --headed --wait-ms 20000 --hotel-key own_demo_a
```

批量抓取 `hotels.json` 里的全部酒店：

```powershell
.venv\Scripts\python.exe main.py scrape-all --channel chrome --headed --wait-ms 20000
```

交互式强制刷新可将本店和竞品限制为最多 2 个并发页面；默认仍为串行，定时任务行为不变：

```powershell
.venv\Scripts\python.exe main.py scrape-all --channel chrome --headless --date 2026-06-20 --days 1 --concurrency 2
```

默认抓取服务器当前日期。指定入住日期：

```powershell
.venv\Scripts\python.exe main.py scrape-all --channel chrome --headed --date 2026-06-20
```

抓取未来 7 天，从当天开始：

```powershell
.venv\Scripts\python.exe main.py scrape-all --channel chrome --headed --days 7
```

从指定日期开始连续 7 天：

```powershell
.venv\Scripts\python.exe main.py scrape-all --channel chrome --headed --date 2026-06-20 --days 7
```

临时抓取一个未写入配置的酒店：

```powershell
.venv\Scripts\python.exe main.py scrape --channel chrome --headed --wait-ms 20000 --url "携程酒店URL" --hotel "酒店名称" --output output\custom_hotel
```

如果 Chrome channel 不可用：

```powershell
.venv\Scripts\python.exe main.py scrape-all --channel chromium --headed --wait-ms 20000
```

输出文件：

- `output/<hotel_key>/ctrip_rooms.json`
- `output/<hotel_key>/ctrip_rooms.jsonl`
- `output/<hotel_key>/ctrip_rooms.md`
- `output/<hotel_key>/raw_json/`
- `output/<hotel_key>/debug/page_text.txt`
- `output/<hotel_key>/debug/page.html`
- `output/<hotel_key>/debug/network_candidates.csv`
- `output/<hotel_key>/debug/run_summary.txt`

当前解析规则优先使用携程自然加载的 `getHotelRoomListInland` 房型列表，将 `saleRoomMap` 的价格映射到 `physicRoomMap` 的真实房型名。解析器不会写死单一路径，会递归扫描 JSON 中的 dict/list，识别同时包含“房型名”和“价格”的对象。通用递归 JSON、`page_text.txt` 和 `page.html` 文本只作为兜底，并会过滤促销、房费、附近酒店推荐等噪声。

`ctrip_rooms.json` 包含 `metadata` 和 `records`，适合一次性给模型读取；`ctrip_rooms.jsonl` 一行一条记录，适合追加、筛选和流式处理；`ctrip_rooms.md` 是面向人工和模型的可读表格。

房型记录字段保持兼容，核心字段如下：

- `stay_date` / `checkin_date` / `date`: 价格对应的入住日期；未指定日期时使用服务器当天。
- `room_name`: 房型名称。
- `normalized_room_type`: 标准房型类别，例如 `big_bed`、`twin`、`esports_big_bed`、`esports_twin`、`other`。
- `price`: 价格，通常为 CNY 数字。
- `available`: 可售状态或余量。
- `breakfast`: 早餐信息。
- `cancel_policy`: 取消政策。
- `source`: 解析来源，例如 `getHotelRoomListInland`、`network_json`、`page_text`、`page_html`。
- `source_type`: 数据类型，例如 `json`、`dom_text`、`status_record`。
- `status`: `success` 表示有有效房型价格；`login_required`、`session_expired`、`captcha_required` 表示当前没有有效房价。

离线调试某个 raw JSON：

```powershell
.venv\Scripts\python.exe -m src.extract output\competitor_demo_a\raw_json\sample.json --hotel "示例竞品酒店 A"
```

该命令会打印解析到的房型数量和前 5 条。若接口返回 `htlSpiderActionErrorCode=4030`，说明是验证码或安全验证返回，不是房型字段路径问题。

## 给 Hermes / Agent 使用

Agent 读取 `output/latest` 中的结构化 JSON / JSONL 文件；同时生成适合复制到飞书文本消息的中文简报 `agent_brief.md`。不生成 Excel。不要同步 `profiles/ctrip_manual_profile`；该目录包含登录态，不能上传、不能泄露。

生成 Hermes 文件：

```powershell
.venv\Scripts\python.exe main.py build-hermes
```

输出文件：

- Hermes 主读取文件：`output/latest/hermes_payload.json`
- 飞书中文简报：`output/latest/agent_brief.md`，展示本店与竞品全部在售房型、同类最低价对比和异常提醒；不展示早餐。
- 运行摘要：`output/latest/run_summary.json`
- 房型历史快照：`output/latest/rooms_all.jsonl`
- 价格差异判断：`output/latest/price_compare.json`，包含 `metadata.fields` 和 `comparisons`

Hermes 建议优先读取：

1. `output/latest/hermes_payload.json`: 主输入，包含本店、竞品、竞品全部在售房型、异常提醒、价格对比和调试文件路径。
2. `output/latest/agent_brief.md`: 适合飞书文本消息的中文简报，展示本店和竞品全部在售房型。
3. `output/latest/price_compare.json`: 相似房型最低在售价对比。
4. `output/latest/rooms_all.jsonl`: 原始房型明细，一行一条，适合程序逐行读取。
5. `output/latest/run_summary.json`: 判断本次是否需要人工登录或验证码处理。

`hermes_payload.json` 中每个酒店对象的关键字段：

- `status`: 酒店本次状态。
- `rooms`: 完整解析房型数组，包含可售、不可售和状态未知记录。
- `available_rooms`: 仅明确可售/可订且价格有效的房型数组。Agent 展示竞品价格时读取 `competitor_hotels[*].available_rooms`。
- `lowest_available_rooms`: 按 `stay_date + normalized_room_type` 汇总后的最低在售有效价列表，用于价格对比。
- `metadata.saved_json_count`: 本次保存的候选 JSON 数量。
- `metadata.page_status`: 页面状态。
- `debug_files`: 对应酒店的 `run_summary.txt`、`page_text.txt`、`network_candidates.csv` 路径。

价格对比规则：

- 对比维度是 `hotel + stay_date + normalized_room_type`。
- 同一家酒店、同一天、同类房型有多个价格时，只取在售/可订且价格有效的最低价。
- 价格为空、0、非数字、已售罄、不可订或可售状态未知的房型不进入 `available_rooms`，也不参与最低价计算。
- `price_compare.json`、`hermes_payload.json` 都保留日期维度。

`summary.need_manual_login=true` 表示需要重新人工登录或处理登录态。`summary.need_parser_update=true` 表示抓到了 JSON 但没有解析出房型，才优先检查 `src/extract.py`。如果酒店状态是 `captcha_required`，需要人工处理验证；程序不会自动绕过。

## 推荐运行流程

1. 人工登录：

```powershell
.venv\Scripts\python.exe main.py login --channel chrome
```

2. 批量抓取 `hotels.json` 里的全部酒店：

```powershell
.venv\Scripts\python.exe main.py scrape-all --channel chrome --headed --wait-ms 20000
```

3. 生成 Hermes 文件：

```powershell
.venv\Scripts\python.exe main.py build-hermes
```

## 检查登录态

```powershell
.venv\Scripts\python.exe main.py status
```

如果抓取输出 `status=session_expired` 或 `status=login_required`，重新执行人工登录命令。

## 验证码、短信或安全验证

如果页面出现验证码、短信验证、人机验证、滑块或安全验证，程序会输出 `status=captcha_required`。请在浏览器中人工处理；程序不会识别、自动处理或绕过验证。

## 结果为空时怎么排查

先看 `output/debug/run_summary.txt` 的下一步建议。

- 如果 raw JSON 里包含 `htlSpiderActionErrorCode=4030`，这是安全验证或验证码返回，不是解析字段问题。
- 如果 `output/raw_json/` 有有效房型 JSON 但结果为空，说明需要优化 `src/extract.py` 的 JSON 解析规则。
- 如果 `output/debug/page_text.txt` 或 `output/debug/page.html` 能看到价格或房型但结果为空，说明需要优化 DOM 文本兜底解析。
- 如果页面状态是 `login_required` 或 `session_expired`，重新执行人工登录。
- 如果页面状态是 `captcha_required`，需要人工处理验证后再运行。

## 修改酒店列表

直接编辑 `hotels.json`：

- `key`: 输出目录和命令里使用的唯一标识，建议用英文和下划线。
- `role`: `own`、`competitor` 或 `unknown`。
- `name`: 结果文件里的酒店名称。
- `url`: 携程酒店详情页 URL。
- `output`: 该酒店的输出目录。

示例文件在 `hotels.example.json`。

## 云服务器手动运行 + 飞书通知人工验证

这一版用于 Linux 云服务器手动运行，不配置定时任务，不创建 `cron`、`systemd` 或后台守护进程。日常抓取使用 headless Chromium；当携程登录态失效、短信验证、验证码或安全验证出现时，程序不会绕过，只通过飞书提醒你进入远程验证会话手动处理。

正常手动执行：

```bash
scripts/run_price_monitor.sh
```

脚本会依次运行：

```bash
.venv/bin/python main.py scrape-all --channel chromium --headless --wait-ms 10000
.venv/bin/python main.py build-hermes
```

`scripts/run_price_monitor.sh` 默认 `WAIT_MS=10000`。如果页面加载慢，可以临时调高：

```bash
WAIT_MS=20000 scripts/run_price_monitor.sh
```

指定日期或连续天数：

```bash
DATE=2026-06-20 scripts/run_price_monitor.sh
DAYS=7 scripts/run_price_monitor.sh
DATE=2026-06-20 DAYS=7 scripts/run_price_monitor.sh
```

如果 `output/latest/run_summary.json` 或 `output/latest/hermes_payload.json` 显示需要人工登录或任意酒店状态为 `login_required`、`session_expired`、`captcha_required`，脚本会调用：

```bash
.venv/bin/python scripts/notify_feishu.py --reason "manual_verification_required"
```

飞书只发送提醒和操作步骤，不发送账号密码、短信验证码、验证码图片、Cookie 或 `profiles/ctrip_manual_profile`。

Hermes 主文件：

```text
output/latest/hermes_payload.json
```

## Linux 安装依赖

系统依赖：

```bash
sudo apt update
sudo apt install -y tigervnc-standalone-server fonts-noto-cjk fonts-wqy-zenhei
```

Python 和 Playwright：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install --with-deps chromium
chmod +x scripts/*.sh scripts/notify_feishu.py
```

## .env 配置

在项目根目录创建 `.env`：

```env
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
FEISHU_SECRET=xxx
PROJECT_DIR=/opt/hotel-ota-monitor
# 远程验证：SSH 隧道转发 5901 后用 VNC Viewer 连接 127.0.0.1::5901
SERVER_SSH_TARGET=your-user@host.example
```

`FEISHU_SECRET` 可选；如果配置了机器人签名密钥，`scripts/notify_feishu.py` 会按飞书签名规则生成 `timestamp` 和 `sign`。`.env` 已被 `.gitignore` 忽略。

## 首次人工登录或远程验证

服务器执行：

```bash
cd /opt/hotel-ota-monitor
scripts/start_verify_session.sh
```

本机 PowerShell 执行 SSH 隧道：

```powershell
ssh -L 5901:127.0.0.1:5901 your-user@host.example
```

本机用 VNC Viewer（如 TigerVNC Viewer）连接：

```text
127.0.0.1::5901
```

在 VNC 画面中手动完成携程登录或验证。`main.py login` 每 15 秒自动检测登录状态：连续 2 次确认「success 且无登录/验证码/会话失效信号」即保存登录态到 `profiles/ctrip_manual_profile` 并自动清理会话；20 分钟未确认以退出码 2 结束。同一时间只允许一个登录会话（锁 `/tmp/ctrip-verify-session.lock`，冲突时退出码 75）。

停止验证会话：

```bash
scripts/stop_verify_session.sh
```

## 手动抓取

服务器手动运行：

```bash
cd /opt/hotel-ota-monitor
scripts/run_price_monitor.sh
```

成功后检查结构化主结果：

```bash
cat output/latest/hermes_payload.json
```

如果希望成功时也发送飞书：

```bash
scripts/run_price_monitor.sh --notify-success
```

## 云服务器安全要求

- 不开放 `5901` 到公网。
- TigerVNC 只监听 `127.0.0.1:5901`，使用 VncAuth 密码。
- 只能通过 SSH 隧道访问 VNC。
- 不在飞书发送账号密码。
- 不在飞书发送验证码。
- 不在飞书发送 Cookie。
- 不在飞书发送 `profiles/ctrip_manual_profile`。
- `profiles/ctrip_manual_profile` 是登录态目录，不能上传、不能泄露。
- 出现验证码时必须人工处理，不自动识别、不自动绕过。
- 可按需要配置 `deploy/systemd/` 下的定时示例。安装前替换示例运行用户、目录和执行时间。

## 飞书通知 dry-run

预览飞书消息，不真实发送：

```bash
.venv/bin/python scripts/notify_feishu.py --dry-run
```

真实发送需要 `.env` 里配置 `FEISHU_WEBHOOK`。

## 差异化调价经验与复盘

`price-decision` 按房型结合 TimescaleDB 事实、历史相似案例和 Mnemosyne 经验输出 `维持`、`小降`、`中降`、`强降` 或 `谨慎上调`。`booking_pace` 尚无字段时输出 `unknown`，不会报错。

写入一条已确认的人工分析或复盘经验：

```bash
python3 main.py memory-add-pricing-lesson --date 2026-07-04 --text "示例：先按房型核对竞品价差，再评估是否调整报价。"
```

复盘某日建议并生成 lesson 草稿（不自动写入 Mnemosyne）：

```bash
python3 main.py price-decision-review --date 2026-07-04
```

草稿默认写入 `output/latest/price_decision_review_<date>.json`；确认后再通过 `memory-add-pricing-lesson --source postmortem` 显式写入。

## Hermes 周复盘

`weekly-review` 基于 TimescaleDB 事实生成按店周复盘；默认由专用 Hermes 分析器输出受 schema 和事实引用约束的结论。Hermes 不可用、超时或结果校验失败时自动降级为确定性规则分析，不写入 Mnemosyne，也不影响 `price-decision` 实时链路。

入住率及环比仅使用 `hotel_daily_operating_metrics.metrics.snapshots.final`。某日缺少 `final` 时，周报会标记该日数据缺失并停止该店的环比计算；不会以 `evening` 或 `own_daily_status` 回退替代。

```bash
python3 main.py weekly-review --week-start 2026-07-06 --week-end 2026-07-12 --dry-run
python3 main.py weekly-review --week-start 2026-07-06 --week-end 2026-07-12 --dry-run --rules-only
```
