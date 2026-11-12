from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from .export import export_rooms, status_record
from .extract import extract_dom_records, extract_json_records, has_hidden_price_signal
from .status import detect_page_status
from .utils import apply_stay_date_to_url, ensure_dir, launch_options, looks_like_json_text, now_iso, safe_filename, write_json


NETWORK_KEYWORDS = [
    "hotel",
    "hotels",
    "room",
    "rooms",
    "price",
    "rate",
    "detail",
    "booking",
    "availability",
    "inventory",
    "roomlist",
    "hotelapi",
    "ota",
    "graphql",
    "query",
    "calendar",
    "product",
]
ROOM_LIST_API_KEYWORDS = ("getHotelRoomListInland", "roomlist")
EARLY_FINISH_EXTRA_MS = 500


async def run_scrape(
    url: str,
    hotel_name: str,
    channel: str,
    profile_dir: str,
    headless: bool,
    wait_ms: int,
    output_dir: str,
    stay_date: str,
) -> dict[str, Any]:
    profile_path = ensure_dir(Path(profile_dir))
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            **launch_options(channel, headless=headless),
        )
        try:
            return await _run_scrape_in_context(
                context=context,
                url=url,
                hotel_name=hotel_name,
                channel=channel,
                profile_path=profile_path,
                headless=headless,
                wait_ms=wait_ms,
                output_dir=output_dir,
                stay_date=stay_date,
            )
        finally:
            await context.close()


async def run_scrape_many(
    hotels: list[Any],
    channel: str,
    profile_dir: str,
    headless: bool,
    wait_ms: int,
    stay_dates: list[str],
    concurrency: int = 1,
) -> list[dict[str, Any]]:
    profile_path = ensure_dir(Path(profile_dir))
    results: list[dict[str, Any]] = []
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            **launch_options(channel, headless=headless),
        )
        try:
            semaphore = asyncio.Semaphore(max(1, min(concurrency, 2)))

            async def scrape_one(hotel: Any, stay_date: str) -> dict[str, Any]:
                target_output = hotel.output if len(stay_dates) == 1 else str(Path(hotel.output) / "dates" / stay_date)
                try:
                    async with semaphore:
                        return await _run_scrape_in_context(
                            context=context,
                            url=hotel.url,
                            hotel_name=hotel.name,
                            channel=channel,
                            profile_path=profile_path,
                            headless=headless,
                            wait_ms=wait_ms,
                            output_dir=target_output,
                            stay_date=stay_date,
                        )
                except Exception as exc:
                    failed = {
                        "run_at": now_iso(),
                        "url": hotel.url,
                        "hotel_name": hotel.name,
                        "stay_date": stay_date,
                        "page_status": "failed",
                        "page_error": str(exc),
                        "result_rows": 0,
                    }
                    record = status_record(hotel.name, "page_error", hotel.url, str(exc))
                    _attach_stay_date([record], stay_date)
                    export_rooms([record], Path(target_output), failed)
                    _write_run_summary(ensure_dir(Path(target_output) / "debug") / "run_summary.txt", failed)
                    return failed

            tasks = [asyncio.create_task(scrape_one(hotel, stay_date)) for hotel in hotels for stay_date in stay_dates]
            flat_results = await asyncio.gather(*tasks)
            width = len(stay_dates)
            for index, hotel in enumerate(hotels):
                date_results = flat_results[index * width:(index + 1) * width]
                results.append(date_results[0] if width == 1 else _aggregate_dated_outputs(hotel, stay_dates, date_results))
        finally:
            await context.close()
    return results


async def _run_scrape_in_context(
    context,
    url: str,
    hotel_name: str,
    channel: str,
    profile_path: Path,
    headless: bool,
    wait_ms: int,
    output_dir: str,
    stay_date: str,
) -> dict[str, Any]:
    out_dir = ensure_dir(Path(output_dir))
    raw_json_dir = ensure_dir(out_dir / "raw_json")
    debug_dir = ensure_dir(out_dir / "debug")
    _clear_previous_capture_files(raw_json_dir, debug_dir)
    candidates: list[dict[str, str]] = []
    json_paths: list[Path] = []
    tasks: list[asyncio.Task] = []
    room_list_seen = asyncio.Event()
    page_error = ""
    target_url = apply_stay_date_to_url(url, stay_date)

    page = await context.new_page()

    def on_response(response) -> None:
        tasks.append(asyncio.create_task(_capture_response_and_signal(response, raw_json_dir, candidates, json_paths, room_list_seen)))

    page.on("response", on_response)
    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        try:
            await asyncio.wait_for(room_list_seen.wait(), timeout=max(wait_ms, 0) / 1000)
            await page.wait_for_timeout(EARLY_FINISH_EXTRA_MS)
        except asyncio.TimeoutError:
            pass
    except Exception as exc:
        page_error = str(exc)

    text = await _safe_text(page)
    html = await _safe_html(page)
    (debug_dir / "page_text.txt").write_text(text, encoding="utf-8")
    (debug_dir / "page.html").write_text(html, encoding="utf-8")

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    _write_candidates_csv(debug_dir / "network_candidates.csv", candidates)
    page_status = detect_page_status(text, html)
    hidden_price = has_hidden_price_signal(json_paths)
    status_value = _page_status_value(page_status.status, page_error, hidden_price)
    await page.close()


    if hidden_price:
        records = [status_record(hotel_name, "login_required", url, "携程价格隐藏，需要重新人工登录")]
    else:
        records = extract_json_records(json_paths, hotel_name, url)
        seen_room_prices = {(record.get("room_name"), record.get("price")) for record in records}
        for dom_record in extract_dom_records(debug_dir / "page_text.txt", hotel_name, url, debug_dir / "page.html"):
            key = (dom_record.get("room_name"), dom_record.get("price"))
            if key not in seen_room_prices:
                records.append(dom_record)
                seen_room_prices.add(key)
    if not records:
        record_status = "parse_failed" if status_value == "success" else status_value
        records.append(status_record(hotel_name, record_status, url, _error_reason(record_status, page_error)))
    _attach_stay_date(records, stay_date)

    summary = {
        "run_at": now_iso(),
        "url": target_url,
        "source_url": url,
        "hotel_name": hotel_name,
        "stay_date": stay_date,
        "checkin_date": stay_date,
        "date": stay_date,
        "channel": channel,
        "profile_dir": str(profile_path),
        "mode": "headless" if headless else "headed",
        "page_status": status_value,
        "candidate_response_count": len(candidates),
        "saved_json_count": len(json_paths),
        "result_rows": len(records),
        "suspected_login_page": str(status_value == "login_required"),
        "suspected_price_hidden": str(hidden_price),
        "suspected_captcha_page": str(page_status.status == "captcha_required"),
        "suspected_session_expired": str(page_status.status == "session_expired"),
        "suspected_empty_data": str(not any(record.get("source_type") != "status_record" for record in records)),
        "next_step": _next_step(status_value, records, len(json_paths), bool(text.strip())),
        "page_error": page_error,
    }
    export_result = export_rooms(records, out_dir, summary)
    row_count = export_result["row_count"]
    summary.update(
        {
            "result_json_path": export_result["json_path"],
            "result_jsonl_path": export_result["jsonl_path"],
            "result_markdown_path": export_result["markdown_path"],
            "result_rows": row_count,
        }
    )
    _write_run_summary(debug_dir / "run_summary.txt", summary)

    print(f"页面状态: {status_value}")
    print(f"JSON: {export_result['json_path']}")
    print(f"JSONL: {export_result['jsonl_path']}")
    print(f"Markdown: {export_result['markdown_path']}")
    print(f"记录数: {row_count}")
    print(f"候选响应: {len(candidates)}")
    print(f"保存 JSON: {len(json_paths)}")
    print(f"调试文件: {debug_dir}")
    if status_value in {"login_required", "session_expired"}:
        print("需要重新执行:")
        print(f".venv\\Scripts\\python.exe main.py login --channel {channel}")

    return summary


def _page_status_value(page_status: str, page_error: str, hidden_price: bool) -> str:
    if hidden_price:
        return "login_required"
    return "page_error" if page_error and page_status == "page_error" else page_status


def _attach_stay_date(records: list[dict], stay_date: str) -> None:
    for record in records:
        record["stay_date"] = stay_date
        record["checkin_date"] = stay_date
        record["date"] = stay_date


def _aggregate_dated_outputs(hotel: Any, stay_dates: list[str], date_results: list[dict[str, Any]]) -> dict[str, Any]:
    base_dir = ensure_dir(Path(hotel.output))
    records: list[dict] = []
    for stay_date in stay_dates:
        result_path = Path(hotel.output) / "dates" / stay_date / "ctrip_rooms.json"
        if not result_path.exists():
            records.append(status_record(hotel.name, "missing_output", hotel.url, f"日期 {stay_date} 缺少输出"))
            _attach_stay_date(records[-1:], stay_date)
            continue
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            rows = payload.get("records", []) if isinstance(payload, dict) else []
            records.extend(row for row in rows if isinstance(row, dict))
        except json.JSONDecodeError as exc:
            records.append(status_record(hotel.name, "page_error", hotel.url, f"日期 {stay_date} 结果 JSON 解析失败: {exc}"))
            _attach_stay_date(records[-1:], stay_date)

    page_statuses = [str(result.get("page_status", "")) for result in date_results]
    aggregate_status = "success" if page_statuses and all(status == "success" for status in page_statuses) else "partial"
    if any(status in {"login_required", "session_expired", "captcha_required"} for status in page_statuses):
        aggregate_status = next(status for status in page_statuses if status in {"login_required", "session_expired", "captcha_required"})
    summary = {
        "run_at": now_iso(),
        "url": hotel.url,
        "hotel_name": hotel.name,
        "stay_dates": stay_dates,
        "date_count": len(stay_dates),
        "page_status": aggregate_status,
        "candidate_response_count": sum(int(result.get("candidate_response_count", 0) or 0) for result in date_results),
        "saved_json_count": sum(int(result.get("saved_json_count", 0) or 0) for result in date_results),
        "result_rows": len(records),
        "date_results": date_results,
    }
    export_result = export_rooms(records, base_dir, summary)
    summary.update(
        {
            "result_json_path": export_result["json_path"],
            "result_jsonl_path": export_result["jsonl_path"],
            "result_markdown_path": export_result["markdown_path"],
            "result_rows": export_result["row_count"],
        }
    )
    debug_dir = ensure_dir(base_dir / "debug")
    _write_run_summary(debug_dir / "run_summary.txt", summary)
    return summary


async def _capture_response_and_signal(
    response,
    raw_json_dir: Path,
    candidates: list[dict[str, str]],
    json_paths: list[Path],
    room_list_seen: asyncio.Event,
) -> None:
    before_count = len(json_paths)
    await _capture_response(response, raw_json_dir, candidates, json_paths)
    if len(json_paths) > before_count and any(keyword.lower() in response.url.lower() for keyword in ROOM_LIST_API_KEYWORDS):
        room_list_seen.set()


async def _capture_response(response, raw_json_dir: Path, candidates: list[dict[str, str]], json_paths: list[Path]) -> None:
    url = response.url
    content_type = response.headers.get("content-type", "")
    haystack = f"{url} {content_type}".lower()
    matched = [keyword for keyword in NETWORK_KEYWORDS if keyword in haystack]
    if not matched:
        return

    row = {
        "captured_at": now_iso(),
        "status": str(response.status),
        "method": response.request.method,
        "resource_type": response.request.resource_type,
        "content_type": content_type,
        "response_url": url,
        "matched_keywords": ",".join(matched),
        "json_saved_path": "",
        "error": "",
    }

    try:
        payload: Any | None = None
        if "json" in content_type.lower():
            payload = await response.json()
        else:
            text = await response.text()
            if looks_like_json_text(text):
                payload = json.loads(text)
        if payload is not None:
            index = len(json_paths) + 1
            json_path = raw_json_dir / f"{index:04d}_{safe_filename(url)}.json"
            write_json(json_path, {"_capture_meta": {"captured_at": row["captured_at"], "response_url": url}, "data": payload})
            row["json_saved_path"] = str(json_path)
            json_paths.append(json_path)
    except Exception as exc:
        row["error"] = str(exc)
    finally:
        candidates.append(row)


def _clear_previous_capture_files(raw_json_dir: Path, debug_dir: Path) -> None:
    for path in raw_json_dir.glob("*.json"):
        path.unlink(missing_ok=True)
    for filename in ("page_text.txt", "page.html", "network_candidates.csv", "run_summary.txt"):
        (debug_dir / filename).unlink(missing_ok=True)


def _write_candidates_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = [
        "captured_at",
        "status",
        "method",
        "resource_type",
        "content_type",
        "response_url",
        "matched_keywords",
        "json_saved_path",
        "error",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_run_summary(path: Path, summary: dict[str, Any]) -> None:
    labels = {
        "run_at": "运行时间",
        "url": "URL",
        "source_url": "原始URL",
        "hotel_name": "酒店名称",
        "stay_date": "入住日期",
        "checkin_date": "checkin_date",
        "date": "date",
        "stay_dates": "入住日期列表",
        "date_count": "日期数量",
        "channel": "channel",
        "profile_dir": "profile_dir",
        "mode": "headed/headless",
        "page_status": "页面状态",
        "candidate_response_count": "候选网络响应数量",
        "saved_json_count": "保存 JSON 数量",
        "result_rows": "结果记录数",
        "result_json_path": "JSON 结果",
        "result_jsonl_path": "JSONL 结果",
        "result_markdown_path": "Markdown 结果",
        "suspected_login_page": "是否疑似登录页",
        "suspected_captcha_page": "是否疑似验证码页",
        "suspected_session_expired": "是否疑似登录态失效",
        "suspected_empty_data": "是否疑似空数据",
        "next_step": "下一步建议",
        "page_error": "页面错误",
    }
    lines = [f"{labels[key]}: {value}" for key, value in summary.items() if key in labels]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _error_reason(status: str, page_error: str) -> str:
    if page_error:
        return f"页面打开失败或读取失败: {page_error}"
    return {
        "login_required": "页面要求登录，未抓取房型价格",
        "captcha_required": "页面要求验证码或安全验证，程序未绕过",
        "session_expired": "登录态可能失效，需要重新人工登录",
        "no_price": "页面正常但未发现价格或房型关键词",
        "parse_failed": "页面或 JSON 可能有数据，但当前解析规则未提取成功",
        "page_error": "页面加载或读取失败",
    }.get(status, status)


def _next_step(status: str, records: list[dict], saved_json_count: int, has_page_text: bool) -> str:
    if status in {"login_required", "session_expired"}:
        return "重新执行 login 模式完成人工登录。"
    if status == "captcha_required":
        return "在浏览器中人工处理验证码、短信或安全验证；程序不会自动绕过。"
    has_data = any(record.get("source_type") != "status_record" for record in records)
    if has_data:
        return "检查 JSON、JSONL 或 Markdown 结果是否符合预期。"
    if saved_json_count:
        return "raw_json 有数据但结果为空，下一步优化 JSON 解析。"
    if has_page_text:
        return "page_text 有内容但结果为空，下一步优化 DOM 解析。"
    return "页面文本和 JSON 都为空，检查网络、登录态或页面加载。"


async def _safe_text(page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=10000)
    except Exception:
        return ""


async def _safe_html(page) -> str:
    try:
        return await page.content()
    except Exception:
        return ""
