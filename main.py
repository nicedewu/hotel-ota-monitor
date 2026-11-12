from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path

from src.config import find_hotel, first_hotel_url, load_hotels
from src.status import profile_status
from src.utils import DEFAULT_CONFIG, DEFAULT_LOGIN_URL, DEFAULT_OUTPUT_DIR, DEFAULT_PROFILE_DIR, ensure_dir, now_iso, resolve_stay_dates


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="携程人工登录版房型/价格抓取测试工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login = subparsers.add_parser("login", help="打开独立浏览器 profile，等待人工登录")
    login.add_argument("--channel", default="chrome", choices=["chrome", "chromium"])
    login.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR))
    login.add_argument("--url", default="")
    login.add_argument("--hotel-key", default="")
    login.add_argument("--config", default=str(DEFAULT_CONFIG))

    scrape = subparsers.add_parser("scrape", help="复用人工登录态监听页面自然响应并导出 JSON/JSONL/Markdown")
    scrape.add_argument("--url", default="")
    scrape.add_argument("--hotel", default="")
    scrape.add_argument("--hotel-key", default="")
    scrape.add_argument("--config", default=str(DEFAULT_CONFIG))
    scrape.add_argument("--channel", default="chrome", choices=["chrome", "chromium"])
    scrape.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR))
    scrape.add_argument("--headed", dest="headless", action="store_false", default=False)
    scrape.add_argument("--headless", dest="headless", action="store_true")
    scrape.add_argument("--wait-ms", type=int, default=20000)
    scrape.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR))
    scrape.add_argument("--date", default="", help="入住日期 YYYY-MM-DD；不传则使用服务器当天")
    scrape.add_argument("--days", type=int, default=1, help="从 --date 或服务器当天开始连续抓取天数")

    scrape_all = subparsers.add_parser("scrape-all", help="按 hotels.json 批量抓取多个酒店")
    scrape_all.add_argument("--config", default=str(DEFAULT_CONFIG))
    scrape_all.add_argument("--channel", default="chrome", choices=["chrome", "chromium"])
    scrape_all.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR))
    scrape_all.add_argument("--headed", dest="headless", action="store_false", default=False)
    scrape_all.add_argument("--headless", dest="headless", action="store_true")
    scrape_all.add_argument("--wait-ms", type=int, default=20000)
    scrape_all.add_argument("--concurrency", type=int, choices=(1, 2), default=1, help="并发页面数；低频抓取最多 2")
    scrape_all.add_argument("--date", default="", help="入住日期 YYYY-MM-DD；不传则使用服务器当天")
    scrape_all.add_argument("--days", type=int, default=1, help="从 --date 或服务器当天开始连续抓取天数")

    scrape_set = subparsers.add_parser("scrape-hotel-set", help="抓取一家自有酒店及其绑定竞品")
    scrape_set.add_argument("--own-hotel-key", required=True)
    scrape_set.add_argument("--config", default=str(DEFAULT_CONFIG))
    scrape_set.add_argument("--channel", default="chrome", choices=["chrome", "chromium"])
    scrape_set.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR))
    scrape_set.add_argument("--headed", dest="headless", action="store_false", default=False)
    scrape_set.add_argument("--headless", dest="headless", action="store_true")
    scrape_set.add_argument("--wait-ms", type=int, default=30000)
    scrape_set.add_argument("--date", default="")
    scrape_set.add_argument("--days", type=int, default=1)

    hermes = subparsers.add_parser("build-hermes", help="聚合 hotels.json 抓取结果，生成 Hermes/Agent 输入文件")
    hermes.add_argument("--config", default=str(DEFAULT_CONFIG))
    hermes.add_argument("--output", default="output/latest")
    hermes.add_argument("--write-db", action="store_true", help="生成 Hermes 文件后追加写入 PostgreSQL")

    db_write = subparsers.add_parser("db-write", help="将 Hermes 可售房型价格追加写入 PostgreSQL")
    db_write.add_argument("--payload", default="output/latest/hermes_payload.json")
    db_write.add_argument("--own-hotel-key", default="")
    db_write.add_argument("--date", default="")

    comparison = subparsers.add_parser("build-price-comparison", help="生成并可选写入单酒店竞品比价")
    comparison.add_argument("--own-hotel-key", required=True)
    comparison.add_argument("--date", default="")
    comparison.add_argument("--days", type=int, default=1)
    comparison.add_argument("--write-db", action="store_true")
    comparison.add_argument("--run-id", default="", help="只读取指定 OTA 抓取批次")
    comparison.add_argument("--no-publish", action="store_true", help="不覆盖 output/latest，供历史批次验证")

    event_sync = subparsers.add_parser("event-intelligence-sync", help="同步官方活动来源到候选活动库")
    event_sync.add_argument("--config", default="config/event_intelligence_sources.json")
    event_sync.add_argument("--dry-run", action="store_true", help="只抓取和解析，不写数据库")
    event_sync.add_argument("--timeout-seconds", type=int, default=15)

    event_candidates = subparsers.add_parser("event-candidates", help="查看待确认活动候选")
    event_candidates.add_argument("--status", default="all")
    event_candidates.add_argument("--limit", type=int, default=50)
    event_candidates.add_argument("--from", dest="date_from", default="", help="活动范围开始日期 YYYY-MM-DD")
    event_candidates.add_argument("--to", dest="date_to", default="", help="活动范围结束日期 YYYY-MM-DD")

    source_health = subparsers.add_parser("event-source-health-check", help="检查关键活动来源连续失败")
    source_health.add_argument("--config", default="config/event_intelligence_sources.json")

    reverse_discovery = subparsers.add_parser("event-reverse-discovery", help="仅对无法解释的高分需求异常执行 Qwen 原生联网搜索")
    reverse_discovery.add_argument("--config", default="config/event_discovery.json")
    reverse_discovery.add_argument("--dry-run", action="store_true")

    forward_discovery = subparsers.add_parser("event-forward-discovery", help="主动搜索未来上海大型活动候选")
    forward_discovery.add_argument("--config", default="config/event_discovery.json")
    forward_discovery.add_argument("--date", default="", help="搜索锚点日期 YYYY-MM-DD；不传则使用服务器当天")
    forward_discovery.add_argument("--days", type=int, default=None, help="覆盖配置中的 horizon_days；默认使用配置值")
    forward_discovery.add_argument("--dry-run", action="store_true")
    forward_discovery.add_argument("--recent", action="store_true", help="只检索新官宣、开票、改期、取消和换场馆；结果仍需官网验证")

    future_event_alert = subparsers.add_parser("future-event-alert", help="生成并可选推送未来高相关活动预警")
    future_event_alert.add_argument("--date", default="", help="预警锚点日期 YYYY-MM-DD；不传则使用服务器当天")
    future_event_alert.add_argument("--days", type=int, default=30)
    future_event_alert.add_argument("--push", action="store_true", help="通过既有分店飞书路由发送预警")
    future_event_alert.add_argument("--dry-run", action="store_true")

    promote_candidate = subparsers.add_parser("promote-event-candidate", help="人工确认后提升候选活动为正式活动")
    promote_candidate.add_argument("--id", type=int, required=True)
    promote_candidate.add_argument("--venue", required=True)
    promote_candidate.add_argument("--demand-level", choices=("high", "medium", "normal"), required=True)
    promote_candidate.add_argument("--stay-start", default="")
    promote_candidate.add_argument("--stay-end", default="")
    promote_candidate.add_argument("--dry-run", action="store_true")


    anomaly_evaluate = subparsers.add_parser("demand-anomaly-evaluate", help="聚合多酒店需求信号并归因；默认影子运行")
    anomaly_evaluate.add_argument("--date", default="", help="入住日期起点 YYYY-MM-DD；不传则使用服务器明天（D+1）")
    anomaly_evaluate.add_argument("--days", type=int, default=30)
    anomaly_evaluate.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)

    anomaly_list = subparsers.add_parser("demand-anomalies", help="查看已持久化的需求异常与活动归因")
    anomaly_list.add_argument("--date", default="", help="入住日期起点 YYYY-MM-DD；不传则使用服务器明天（D+1）")
    anomaly_list.add_argument("--days", type=int, default=30)

    pms_snapshot = subparsers.add_parser("pms-snapshot", help="写入当天 PMS 经营快照到 own_daily_status")
    pms_snapshot.add_argument("--date", default="", help="快照日期 YYYY-MM-DD；不传则使用服务器当天")
    pms_snapshot.add_argument("--days", type=int, default=1, help="定时任务只支持 1")
    pms_snapshot.add_argument("--own-hotel-key", default="own_demo_a")

    price_decision = subparsers.add_parser("price-decision", help="基于 OTA 价格和经营快照生成调价建议")
    price_decision.add_argument("--date", default="", help="入住日期 YYYY-MM-DD；不传则使用服务器当天")
    price_decision.add_argument("--days", type=int, default=1, help="连续生成建议的天数")
    price_decision.add_argument("--own-hotel-key", default="own_demo_a")
    price_decision.add_argument("--config", default=str(DEFAULT_CONFIG))
    price_decision.add_argument("--output", default="output/latest/price_decision_brief.md")
    price_decision.add_argument("--dry-run", action="store_true", help="只生成建议，不写 price_decisions")

    memory_lesson = subparsers.add_parser("memory-add-pricing-lesson", help="把人工调价分析或复盘结论写入 Mnemosyne")
    memory_lesson.add_argument("--date", required=True, help="入住日期 YYYY-MM-DD")
    memory_lesson.add_argument("--text", required=True)
    memory_lesson.add_argument("--own-hotel-key", default="own_demo_a")
    memory_lesson.add_argument("--source", choices=["feishu_analysis", "manual", "postmortem"], default="manual")
    memory_lesson.add_argument("--event-name", default="", help="不传则从展会事实表自动补充")
    memory_lesson.add_argument("--room-category", default="")

    decision_review = subparsers.add_parser("price-decision-review", help="按实际销售结果复盘调价建议并生成 lesson 草稿")
    decision_review.add_argument("--date", required=True, help="入住日期 YYYY-MM-DD")
    decision_review.add_argument("--own-hotel-key", default="own_demo_a")
    decision_review.add_argument("--output", default="")

    weekly_review = subparsers.add_parser("weekly-pricing-review", help="从 TimescaleDB 生成周度价格复盘")
    weekly_review.add_argument("--week-start", required=True, help="周期开始日期 YYYY-MM-DD")
    weekly_review.add_argument("--week-end", required=True, help="周期结束日期 YYYY-MM-DD")

    weekly_review = subparsers.add_parser("weekly-review", help="按酒店生成经营周复盘")
    week_group = weekly_review.add_mutually_exclusive_group(required=True)
    week_group.add_argument("--auto-last-week", action="store_true", help="自动复盘上一自然周")
    week_group.add_argument("--week-start", default="", help="周期开始日期 YYYY-MM-DD")
    weekly_review.add_argument("--week-end", default="", help="周期结束日期 YYYY-MM-DD")
    weekly_review.add_argument("--hotel-key", default="all", help="指定酒店 key 或 all")
    weekly_review.add_argument("--dry-run", action="store_true", default=True, help="默认测试模式，不推送、不写 weekly_reviews")
    weekly_review.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    notify_group = weekly_review.add_mutually_exclusive_group()
    notify_group.add_argument("--notify", action="store_true", help="生成后立即推送；dry-run 下只预览")
    notify_group.add_argument("--notify-existing", action="store_true", help="只推送已生成且通过校验的周报")
    notify_group.add_argument("--notify-admin-existing", action="store_true", help="只从 weekly_admin_reviews 推送管理员合并周报")
    notify_group.add_argument("--admin-preview", action="store_true", help="只读预览 weekly_admin_reviews 中的管理员合并周报")
    weekly_review.add_argument("--write-db", action="store_true", help="允许写 weekly_reviews；dry-run 下不写")
    weekly_review.add_argument("--retry-failed-only", action="store_true", help="只重试缺失、blocked 或生成失败的门店")
    weekly_review.add_argument("--force-regenerate", action="store_true", help="重新生成未推送报告；不能绕过 pushed 幂等状态")
    weekly_review.add_argument("--preflight-only", action="store_true", help="只读检查周复盘正式入住率样本")
    weekly_review.add_argument("--require-complete-final", action="store_true", help="要求本周和上周 final 入住率均为 7/7，否则阻断生成")
    weekly_review.add_argument("--rules-only", action="store_true", help="不调用 Hermes，使用确定性规则")
    weekly_review.add_argument("--confirm-send", action="store_true", help="确认管理员周报真实推送")
    weekly_review.add_argument("--admin-overview", action=argparse.BooleanOptionalAction, default=True)
    weekly_review.add_argument("--output-dir", default="output/weekly_reviews")
    weekly_review.add_argument("--config", default=str(DEFAULT_CONFIG))

    data_health = subparsers.add_parser("data-health", help="只读检查每日入库和归档情况")
    data_health.add_argument("--date", required=True, help="日期 YYYY-MM-DD")
    data_health.add_argument("--hotel-key", default="all", help="指定酒店 key 或 all")
    data_health.add_argument("--config", default=str(DEFAULT_CONFIG))

    report = subparsers.add_parser("business-snapshot-report", help="生成当天经营快照 JSON 和飞书消息")
    report.add_argument("--date", default="", help="快照日期 YYYY-MM-DD；不传则使用服务器当天")
    report.add_argument("--days", type=int, default=1)
    report.add_argument("--detail-level", choices=["full"], default="full")
    report.add_argument("--config", default=str(DEFAULT_CONFIG))
    report.add_argument("--output", default="output/latest/business_snapshot_report.json")
    report.add_argument("--own-hotel-key", default="own_demo_a")

    scheduled = subparsers.add_parser("scheduled-snapshot", help="按自有酒店串行执行经营快照")
    target = scheduled.add_mutually_exclusive_group(required=True)
    target.add_argument("--own-hotel-key", default="")
    target.add_argument("--all-active", action="store_true")
    target.add_argument("--all-own-hotels", dest="all_active", action="store_true")
    push = scheduled.add_mutually_exclusive_group()
    push.add_argument("--push", dest="push", action="store_true")
    push.add_argument("--no-push", dest="push", action="store_false")
    scheduled.set_defaults(push=False)
    scheduled.add_argument("--date", default="")
    scheduled.add_argument("--days", type=int, default=1)
    scheduled.add_argument("--config", default=str(DEFAULT_CONFIG))
    scheduled.add_argument("--channel", default="chrome", choices=["chrome", "chromium"])
    scheduled.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR))
    scheduled.add_argument("--wait-ms", type=int, default=30000)
    scheduled.add_argument("--dry-run", action="store_true")
    scheduled.add_argument("--stagger-seconds", type=int, default=0)
    scheduled.add_argument("--slot", choices=["morning", "afternoon", "evening", "event"], default="morning")
    scheduled.add_argument("--skip-pms-snapshot", action="store_true")

    status = subparsers.add_parser("status", help="检查 profile 目录是否可能已有登录态")
    status.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "login":
        from src.login import run_login

        url = resolve_login_url(args.url, args.hotel_key, args.config)
        asyncio.run(run_login(url, args.channel, args.profile_dir))
    elif args.command == "scrape":
        from src.capture import run_scrape

        url, hotel_name, output_dir = resolve_scrape_target(
            url=args.url,
            hotel_name=args.hotel,
            hotel_key=args.hotel_key,
            config=args.config,
            output=args.output,
        )
        stay_dates = resolve_stay_dates(args.date or None, args.days)
        if len(stay_dates) > 1:
            raise SystemExit("scrape 单酒店模式暂只支持一个日期；多日期请使用 scrape-all --days")
        asyncio.run(
            run_scrape(
                url=url,
                hotel_name=hotel_name,
                channel=args.channel,
                profile_dir=args.profile_dir,
                headless=args.headless,
                wait_ms=args.wait_ms,
                output_dir=output_dir,
                stay_date=stay_dates[0],
            )
        )
    elif args.command == "scrape-all":
        from src.capture import run_scrape_many

        hotels = load_hotels(args.config)
        if not hotels:
            raise SystemExit(f"配置文件 {args.config} 没有可抓取的酒店")
        valid_hotels = []
        stay_dates = resolve_stay_dates(args.date or None, args.days)
        summary = {
            "run_at": now_iso(),
            "config": args.config,
            "hotel_count": len(hotels),
            "stay_dates": stay_dates,
            "date_count": len(stay_dates),
            "results": [],
        }
        for hotel in hotels:
            print(f"\n=== {hotel.key}: {hotel.name} ===")
            if not hotel.url:
                item = {"key": hotel.key, "name": hotel.name, "status": "skipped_missing_url", "output": hotel.output}
                summary["results"].append(item)
                print("跳过：hotels.json 中缺少 url")
                continue
            valid_hotels.append(hotel)
        if valid_hotels:
            results = asyncio.run(
                run_scrape_many(
                    hotels=valid_hotels,
                    channel=args.channel,
                    profile_dir=args.profile_dir,
                    headless=args.headless,
                    wait_ms=args.wait_ms,
                    stay_dates=stay_dates,
                    concurrency=args.concurrency,
                )
            )
            for hotel, result in zip(valid_hotels, results):
                item = {"key": hotel.key, "name": hotel.name, "status": result.get("page_status", ""), "output": hotel.output}
                if result.get("page_error"):
                    item["error"] = result.get("page_error")
                summary["results"].append(item)
        latest = ensure_dir(Path("output/latest"))
        (latest / "scrape_all_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nscrape-all 摘要: {latest / 'scrape_all_summary.json'}")
    elif args.command == "scrape-hotel-set":
        from src.multi_hotel import scrape_hotel_set

        payload = scrape_hotel_set(
            args.own_hotel_key,
            channel=args.channel,
            profile_dir=args.profile_dir,
            headless=args.headless,
            wait_ms=args.wait_ms,
            stay_date=args.date or None,
            days=args.days,
            config_path=args.config,
        )
        print(f"已输出: {payload}")
    elif args.command == "build-hermes":
        from src.hermes import build_hermes

        build_hermes(args.config, args.output)
        if args.write_db:
            _write_db_or_exit(str(Path(args.output) / "hermes_payload.json"))
    elif args.command == "db-write":
        payload = args.payload
        if args.own_hotel_key:
            current_date = resolve_stay_dates(args.date or None, 1)[0]
            payload = str(Path("output/hotel_sets") / args.own_hotel_key / current_date / "hermes_payload.json")
        _write_db_or_exit(payload)
    elif args.command == "build-price-comparison":
        from src.multi_hotel import build_price_comparisons

        comparisons = build_price_comparisons(
            args.own_hotel_key,
            stay_date=args.date or None,
            days=args.days,
            write_db=args.write_db,
            run_id=args.run_id or None,
            publish_latest=not args.no_publish,
        )
        print(f"price-comparison rows={len(comparisons)} write_db={args.write_db}")
    elif args.command == "event-intelligence-sync":
        from src.event_intelligence import EventIntelligenceError, sync_event_sources

        try:
            summary = sync_event_sources(
                args.config,
                dry_run=args.dry_run,
                timeout_seconds=args.timeout_seconds,
            )
        except EventIntelligenceError as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    elif args.command == "event-candidates":
        from src.event_intelligence import EventIntelligenceError, list_event_candidates

        try:
            candidates = list_event_candidates(
                args.status,
                args.limit,
                date_from=args.date_from,
                date_to=args.date_to,
            )
        except EventIntelligenceError as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps(candidates, ensure_ascii=False, indent=2, default=str))
    elif args.command == "promote-event-candidate":
        from src.event_intelligence import EventIntelligenceError, promote_event_candidate

        try:
            result = promote_event_candidate(
                args.id,
                venue=args.venue,
                demand_level=args.demand_level,
                stay_start=args.stay_start,
                stay_end=args.stay_end,
                dry_run=args.dry_run,
            )
        except EventIntelligenceError as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    elif args.command == "event-source-health-check":
        from src.event_source_health import EventSourceHealthError, critical_source_failures
        from src.event_intelligence import _load_sources

        try:
            critical = [str(source["key"]) for source in _load_sources(args.config) if source.get("critical")]
            failures = critical_source_failures(critical)
        except (EventSourceHealthError, EventIntelligenceError) as exc:
            raise SystemExit(str(exc)) from exc
        if failures:
            raise SystemExit(json.dumps({"critical_source_failures": failures}, ensure_ascii=False, default=str))
        print(json.dumps({"critical_source_failures": []}, ensure_ascii=False))
    elif args.command == "event-reverse-discovery":
        from src.event_discovery import EventDiscoveryError, run_reverse_discovery
        from src.demand_attribution import DemandAttributionError, evaluate_demand_anomalies

        try:
            result = run_reverse_discovery(args.config, dry_run=args.dry_run)
            if result.get("stay_dates") and not args.dry_run:
                for stay_date in result["stay_dates"]:
                    evaluate_demand_anomalies(stay_date, days=1, dry_run=False)
        except (EventDiscoveryError, DemandAttributionError) as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    elif args.command == "event-forward-discovery":
        from src.event_discovery import EventDiscoveryError, run_forward_discovery

        try:
            anchor_date = date.fromisoformat(args.date) if args.date else date.today()
            result = run_forward_discovery(args.config, anchor_date=anchor_date, days=args.days, dry_run=args.dry_run, recent=args.recent)
        except (EventDiscoveryError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    elif args.command == "future-event-alert":
        from src.future_event_alert import FutureEventAlertError, run_future_event_alerts

        try:
            anchor_date = date.fromisoformat(args.date) if args.date else date.today()
            result = run_future_event_alerts(anchor_date=anchor_date, days=args.days, push=args.push, dry_run=args.dry_run)
        except (FutureEventAlertError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    elif args.command == "demand-anomaly-evaluate":
        from src.demand_attribution import DemandAttributionError, evaluate_demand_anomalies

        target_date = args.date or resolve_stay_dates(None, 2)[1]
        try:
            results = evaluate_demand_anomalies(target_date, days=args.days, dry_run=args.dry_run)
        except DemandAttributionError as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    elif args.command == "demand-anomalies":
        from src.demand_attribution import DemandAttributionError, list_demand_anomalies

        target_date = args.date or resolve_stay_dates(None, 2)[1]
        try:
            results = list_demand_anomalies(target_date, days=args.days)
        except DemandAttributionError as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    elif args.command == "pms-snapshot":
        _pms_snapshot_or_exit(args.date or None, args.days, args.own_hotel_key)
    elif args.command == "price-decision":
        _price_decision_or_exit(args.date or None, args.days, args.own_hotel_key, args.output, args.config, args.dry_run)
    elif args.command == "memory-add-pricing-lesson":
        _memory_add_pricing_lesson_or_exit(args.date, args.text, args.own_hotel_key, args.source, args.event_name, args.room_category)
    elif args.command == "price-decision-review":
        _price_decision_review_or_exit(args.date, args.own_hotel_key, args.output)
    elif args.command == "weekly-pricing-review":
        _weekly_pricing_review_or_exit(args.week_start, args.week_end)
    elif args.command == "weekly-review":
        _weekly_review_or_exit(
            args.week_start,
            args.week_end,
            args.auto_last_week,
            args.hotel_key,
            args.dry_run,
            args.notify,
            args.notify_existing,
            args.notify_admin_existing,
            args.admin_preview,
            args.write_db,
            args.admin_overview,
            args.retry_failed_only,
            args.force_regenerate,
            args.preflight_only,
            args.require_complete_final,
            args.rules_only,
            args.confirm_send,
            args.output_dir,
            args.config,
        )
    elif args.command == "data-health":
        _data_health_or_exit(args.date, args.hotel_key, args.config)
    elif args.command == "business-snapshot-report":
        _business_snapshot_report_or_exit(args.date or None, args.days, args.detail_level, args.config, args.output, args.own_hotel_key)
    elif args.command == "scheduled-snapshot":
        from src.multi_hotel import run_scheduled_snapshot

        results = run_scheduled_snapshot(
            own_hotel_key=args.own_hotel_key or None,
            all_active=args.all_active,
            stay_date=args.date or None,
            days=args.days,
            push=args.push,
            channel=args.channel,
            profile_dir=args.profile_dir,
            wait_ms=args.wait_ms,
            config_path=args.config,
            dry_run=args.dry_run,
            stagger_seconds=args.stagger_seconds,
            slot=args.slot,
            skip_pms_snapshot=args.skip_pms_snapshot,
        )
        print(json.dumps(results, ensure_ascii=False, indent=2))
        if any(result["status"] == "failed" for result in results):
            raise SystemExit("部分酒店经营快照执行失败")
    elif args.command == "status":
        print(profile_status(args.profile_dir))


def resolve_login_url(url: str, hotel_key: str, config: str) -> str:
    if url:
        return url
    if hotel_key:
        return find_hotel(config, hotel_key).url
    return first_hotel_url(config) or DEFAULT_LOGIN_URL


def resolve_scrape_target(url: str, hotel_name: str, hotel_key: str, config: str, output: str) -> tuple[str, str, str]:
    if hotel_key:
        hotel = find_hotel(config, hotel_key)
        if not hotel.url:
            raise SystemExit(f"hotel-key={hotel_key} 缺少 url，无法抓取")
        return hotel.url, hotel.name, hotel.output if output == str(DEFAULT_OUTPUT_DIR) else output
    if not url or not hotel_name:
        raise SystemExit("scrape 需要传 --hotel-key，或同时传 --url 和 --hotel")
    return url, hotel_name, output


def _write_db_or_exit(payload_path: str) -> None:
    from src.db_writer import DBWriterError, write_ota_room_prices_from_payload

    try:
        inserted = write_ota_room_prices_from_payload(payload_path)
    except DBWriterError as exc:
        raise SystemExit(str(exc)) from exc
    if inserted == 0:
        print("没有可写入的可售房型")
    else:
        print(f"已写入 ota_room_prices: {inserted} 行")


def _pms_snapshot_or_exit(stay_date: str | None, days: int, own_hotel_key: str) -> None:
    from src.pms_snapshot import PmsSnapshotError, write_pms_snapshot

    try:
        write_pms_snapshot(stay_date=stay_date, days=days, own_hotel_key=own_hotel_key)
    except PmsSnapshotError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"已写入 own_daily_status: hotel_key={own_hotel_key}")


def _price_decision_or_exit(stay_date: str | None, days: int, own_hotel_key: str, output_path: str, config_path: str, dry_run: bool = False) -> None:
    from src.price_decision import DBWriterError, generate_price_decision_brief

    try:
        decisions, _, run_id, _ = generate_price_decision_brief(
            stay_date=stay_date,
            days=days,
            own_hotel_key=own_hotel_key,
            output_path=output_path,
            config_path=config_path,
            persist=not dry_run,
        )
    except (DBWriterError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"price-decision run_id={run_id} inserted={0 if dry_run else len(decisions)} dry_run={dry_run}")
    print(f"已输出: {output_path}")


def _memory_add_pricing_lesson_or_exit(
    stay_date: str,
    text: str,
    own_hotel_key: str,
    source: str,
    event_name: str,
    room_category: str,
) -> None:
    from src.price_decision import DBWriterError, add_pricing_lesson

    try:
        result = add_pricing_lesson(
            stay_date=stay_date,
            text=text,
            own_hotel_key=own_hotel_key,
            source=source,
            event_name=event_name,
            room_category=room_category,
        )
    except DBWriterError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _price_decision_review_or_exit(stay_date: str, own_hotel_key: str, output_path: str) -> None:
    from src.price_decision import DBWriterError, review_price_decisions

    try:
        report = review_price_decisions(stay_date=stay_date, own_hotel_key=own_hotel_key, output_path=output_path)
    except DBWriterError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _weekly_pricing_review_or_exit(week_start: str, week_end: str) -> None:
    from src.weekly_pricing_review import WeeklyPricingReviewError, build_weekly_pricing_review

    try:
        report = build_weekly_pricing_review(week_start, week_end)
    except WeeklyPricingReviewError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print("已输出: output/latest/weekly_pricing_review_payload.json")
    print("已输出: output/latest/weekly_pricing_review.md")


def _weekly_review_or_exit(
    week_start: str,
    week_end: str,
    auto_last_week: bool,
    hotel_key: str,
    dry_run: bool,
    notify: bool,
    notify_existing: bool,
    notify_admin_existing: bool,
    admin_preview: bool,
    write_db: bool,
    admin_overview: bool,
    retry_failed_only: bool,
    force_regenerate: bool,
    preflight_only: bool,
    require_complete_final: bool,
    no_llm: bool,
    confirm_send: bool,
    output_dir: str,
    config_path: str,
) -> None:
    from src.weekly_review import (
        WeeklyReviewError,
        notify_existing_admin_review,
        notify_existing_weekly_reviews,
        preflight_weekly_review,
        preview_admin_review,
        run_weekly_review,
    )

    try:
        common = dict(week_start=week_start, week_end=week_end, auto_last_week=auto_last_week)
        if preflight_only:
            result = preflight_weekly_review(**common, hotel_key=hotel_key, config_path=config_path)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if admin_preview:
            result = preview_admin_review(**common)
        elif notify_admin_existing:
            result = notify_existing_admin_review(**common, dry_run=dry_run, output_dir=output_dir, confirm_send=confirm_send)
        elif notify_existing:
            result = notify_existing_weekly_reviews(**common, hotel_key=hotel_key, dry_run=dry_run, output_dir=output_dir, config_path=config_path)
        else:
            result = run_weekly_review(
                **common, hotel_key=hotel_key, dry_run=dry_run, notify=notify,
                write_db=write_db, admin_overview=admin_overview,
                output_dir=output_dir, config_path=config_path,
                retry_failed_only=retry_failed_only, force_regenerate=force_regenerate,
                require_complete_final=require_complete_final, no_llm=no_llm,
            )
    except WeeklyReviewError as exc:
        raise SystemExit(str(exc)) from exc
    for key in (
        "run_id",
        "period",
        "hotels_processed",
        "reports_written",
        "admin_overview_written",
        "notify_status",
        "db_write_status",
        "warnings",
        "expected_hotel_count",
        "actual_hotel_count",
        "generation_status",
    ):
        if key not in result:
            continue
        label = "weekly-review run_id" if key == "run_id" else key
        print(f"{label}={result[key]}")
    if result.get("warning_messages"):
        print(json.dumps({"warning_messages": result["warning_messages"]}, ensure_ascii=False, indent=2))
    if result.get("hotel_results"):
        print(json.dumps({"hotel_results": result["hotel_results"]}, ensure_ascii=False, indent=2))
    if admin_preview:
        print(result["admin_overview"]["report_text"])


def _data_health_or_exit(stay_date: str, hotel_key: str, config_path: str) -> None:
    from src.weekly_review import WeeklyReviewError, run_data_health

    try:
        result = run_data_health(stay_date=stay_date, hotel_key=hotel_key, config_path=config_path)
    except WeeklyReviewError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"data-health date={result['date']}")
    for key in (
        "hotels_checked",
        "own_daily_status_rows",
        "ota_room_prices_rows",
        "price_comparisons_rows",
        "price_decisions_rows",
        "warnings",
    ):
        value = len(result[key]) if key == "warnings" else result[key]
        print(f"{key}={value}")
    if result.get("warnings"):
        print(json.dumps({"warning_messages": result["warnings"]}, ensure_ascii=False, indent=2))


def _business_snapshot_report_or_exit(stay_date: str | None, days: int, detail_level: str, config_path: str, output_path: str, own_hotel_key: str) -> None:
    from src.business_snapshot import BusinessSnapshotError, build_business_snapshot_report

    try:
        report = build_business_snapshot_report(
            stay_date=stay_date,
            days=days,
            detail_level=detail_level,
            config_path=config_path,
            output_path=output_path,
            own_hotel_key=own_hotel_key,
        )
    except BusinessSnapshotError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"已输出: {output_path}")
    print(f"状态: {report['metadata']['status']}")


if __name__ == "__main__":
    main()
