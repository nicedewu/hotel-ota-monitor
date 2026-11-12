from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from datetime import date
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


class QwenSearchError(RuntimeError):
    """A safe operator-facing failure from the native Qwen web-search API."""


_ALLOWED_SHARED_HOSTS = {"dashscope.aliyuncs.com"}
_WORKSPACE_HOST_SUFFIX = ".cn-beijing.maas.aliyuncs.com"


def _api_base(config: dict[str, Any]) -> str:
    qwen = config.get("qwen") if isinstance(config.get("qwen"), dict) else {}
    value = str(os.getenv("QWEN_SEARCH_API_BASE") or qwen.get("api_base") or "").strip().rstrip("/")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.path.rstrip("/") != "/api/v1"
        or (host not in _ALLOWED_SHARED_HOSTS and not host.endswith(_WORKSPACE_HOST_SUFFIX))
    ):
        raise QwenSearchError("QWEN_SEARCH_API_BASE 必须是北京地域的 DashScope API 地址")
    return value


def _api_key(config: dict[str, Any]) -> str:
    qwen = config.get("qwen") if isinstance(config.get("qwen"), dict) else {}
    env_name = str(qwen.get("api_key_env") or "DASHSCOPE_API_KEY").strip()
    key = str(os.getenv(env_name) or "").strip()
    if not key:
        raise QwenSearchError(f"缺少 {env_name}，无法调用 Qwen 原生联网搜索")
    return key


def _qwen_config(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("qwen")
    if not isinstance(value, dict):
        raise QwenSearchError("活动发现缺少 qwen 配置")
    model = str(value.get("model") or "").strip()
    if model != "qwen3.8-flash":
        raise QwenSearchError("活动发现仅允许使用 qwen3.8-flash")
    return value


def _valid_result_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _result_summary(item: dict[str, Any]) -> str:
    values = (
        item.get("snippet"),
        item.get("summary"),
        item.get("content"),
        item.get("text"),
    )
    return "\n".join(str(value).strip() for value in values if str(value or "").strip())[:12_000]


def _normalise_results(raw_results: object, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(raw_results, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = _valid_result_url(item.get("url") or item.get("link"))
        if not url or url in seen:
            continue
        seen.add(url)
        normalised: dict[str, Any] = {
            "title": str(item.get("title") or item.get("name") or "").strip(),
            "summary": _result_summary(item),
            "url": url,
            "published_at": str(item.get("published_at") or item.get("publish_time") or "").strip(),
            "provider": "qwen_native",
        }
        try:
            source_index = int(item.get("index"))
        except (TypeError, ValueError):
            source_index = 0
        if source_index > 0:
            normalised["source_index"] = source_index
        result.append(normalised)
        if len(result) >= limit:
            break
    return result


def _message_text(output: object) -> str:
    if not isinstance(output, dict):
        return ""
    choices = output.get("choices")
    if not isinstance(choices, list):
        return ""
    parts: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
    return "".join(parts)


def _parse_sse_response(
    lines: Iterable[bytes | str], *, limit: int
) -> tuple[list[dict[str, Any]], str]:
    """Collect both generated content and source metadata from a Qwen SSE response."""

    collected: list[dict[str, Any]] = []
    answer_parts: list[str] = []
    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("code"):
            code = str(event.get("code") or "unknown")[:80]
            raise QwenSearchError(f"Qwen 原生联网搜索返回错误：{code}")
        output = event.get("output") if isinstance(event, dict) else None
        answer = _message_text(output)
        if answer:
            answer_parts.append(answer)
        search_info = output.get("search_info") if isinstance(output, dict) else None
        source_results = search_info.get("search_results") if isinstance(search_info, dict) else None
        if isinstance(source_results, list) and source_results:
            collected.extend(source_results)
    return _normalise_results(collected, limit=limit), "".join(answer_parts).strip()


def _search_results_from_sse_lines(lines: Iterable[bytes | str], *, limit: int) -> list[dict[str, Any]]:
    """Backward-compatible source-list parser used by existing callers and tests."""

    sources, _answer = _parse_sse_response(lines, limit=limit)
    return sources


def _structured_event_results(
    answer_text: str,
    sources: list[dict[str, Any]],
    *,
    limit: int,
    hotel_keys: set[str] | None = None,
    date_window: tuple[date, date] | None = None,
) -> list[dict[str, Any]] | None:
    if not answer_text:
        return None
    try:
        payload = json.loads(answer_text)
    except json.JSONDecodeError:
        return None
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        return None
    by_index = {
        int(source["source_index"]): source
        for source in sources
        if isinstance(source.get("source_index"), int)
    }
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        name = str(event.get("event_name") or "").strip()
        indexes = event.get("source_indexes")
        if not name or not isinstance(indexes, list):
            continue
        referenced = [by_index[index] for index in indexes if isinstance(index, int) and index in by_index]
        if not referenced:
            continue
        primary = referenced[0]
        start_date = str(event.get("event_start_date") or "").strip()
        venue = str(event.get("venue") or "").strip()
        if date_window is not None:
            try:
                start = date.fromisoformat(start_date)
                end = date.fromisoformat(str(event.get("event_end_date") or start_date))
            except ValueError:
                continue
            if end < start or end < date_window[0] or start > date_window[1]:
                continue
        offline_mode = str(event.get("offline_mode") or "unknown").strip()
        if offline_mode.lower() in {"online", "online-only", "线上", "纯线上"}:
            continue
        if "非上海" in venue:
            continue
        hotel_relevance: list[dict[str, Any]] = []
        for item in event.get("hotel_relevance") or []:
            if not isinstance(item, dict):
                continue
            hotel_key = str(item.get("hotel_key") or "").strip()
            level = str(item.get("impact_level") or "unknown").strip().lower()
            reason = str(item.get("reason") or "").strip()
            indexes = [index for index in (item.get("source_indexes") or [])
                       if isinstance(index, int) and index in event["source_indexes"] and index in by_index]
            if not hotel_key or (hotel_keys is not None and hotel_key not in hotel_keys):
                continue
            if level not in {"high", "medium", "low", "unknown"}:
                level = "unknown"
            if level in {"high", "medium"} and (not reason or not indexes):
                level = "unknown"
            hotel_relevance.append({"hotel_key": hotel_key, "impact_level": level,
                                    "reason": reason, "source_indexes": indexes})
        if hotel_keys:
            present = {item["hotel_key"] for item in hotel_relevance}
            hotel_relevance.extend({"hotel_key": key, "impact_level": "unknown",
                                    "reason": "未返回本店住宿关联依据", "source_indexes": []}
                                   for key in sorted(hotel_keys - present))
        identity = (name.lower(), start_date, venue.lower())
        if identity in seen:
            continue
        seen.add(identity)
        result: dict[str, Any] = {
            "title": name,
            "aliases": [
                str(alias).strip()
                for alias in (event.get("aliases") or [])
                if isinstance(alias, str) and str(alias).strip()
            ],
            "summary": str(event.get("summary") or event.get("impact_reason") or "").strip(),
            "url": primary["url"],
            "published_at": primary.get("published_at", ""),
            "provider": "qwen_native",
            "event_start_date": start_date,
            "event_end_date": str(event.get("event_end_date") or start_date).strip(),
            "venue": venue,
            "category": str(event.get("category") or "other").strip(),
            "event_status": str(event.get("event_status") or "discovered").strip(),
            "offline_mode": offline_mode,
            "impact_level": str(event.get("impact_level") or "unknown").strip(),
            "impact_reason": str(event.get("impact_reason") or "").strip(),
            "hotel_relevance": hotel_relevance,
            "source_urls": [source["url"] for source in referenced],
            "source_indexes": [source["source_index"] for source in referenced],
            "source_evidence": [
                {
                    "url": source["url"],
                    "title": source.get("title", ""),
                    "summary": source.get("summary", ""),
                    "published_at": source.get("published_at", ""),
                }
                for source in referenced
            ],
        }
        results.append(result)
        if len(results) >= limit:
            break
    return results


def _event_response_format() -> dict[str, Any]:
    nullable_string = {"type": ["string", "null"]}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "shanghai_event_search",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "events": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "event_name": {"type": "string"},
                                "aliases": {"type": "array", "items": {"type": "string"}},
                                "event_start_date": nullable_string,
                                "event_end_date": nullable_string,
                                "venue": nullable_string,
                                "category": {"type": "string", "enum": ["esports", "sports", "performance", "acg", "exhibition", "other"]},
                                "event_status": {"type": "string"},
                                "offline_mode": {"type": "string"},
                                "summary": {"type": "string"},
                                "impact_level": {"type": "string", "enum": ["high", "medium", "low", "unknown"]},
                                "impact_reason": {"type": "string"},
                                "hotel_relevance": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "hotel_key": {"type": "string"},
                                            "impact_level": {"type": "string", "enum": ["high", "medium", "low", "unknown"]},
                                            "reason": {"type": "string"},
                                            "source_indexes": {"type": "array", "items": {"type": "integer"}},
                                        },
                                        "required": ["hotel_key", "impact_level", "reason", "source_indexes"],
                                        "additionalProperties": False,
                                    },
                                },
                                "source_indexes": {"type": "array", "items": {"type": "integer"}},
                            },
                            "required": [
                                "event_name", "aliases", "event_start_date", "event_end_date", "venue",
                                "category", "event_status", "offline_mode", "summary",
                                "impact_level", "impact_reason", "hotel_relevance", "source_indexes",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "overview": {"type": "string"},
                    "key_periods": {"type": "array", "items": {"type": "string"}},
                    "operating_advice": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["events", "overview", "key_periods", "operating_advice"],
                "additionalProperties": False,
            },
        },
    }


def _request_payload(query: str, qwen: dict[str, Any], *, recent: bool, strategy: str) -> bytes:
    suffix = "仅检索最近新官宣、开票、改期、取消或换场馆信息。" if recent else ""
    instructions = (
        "请联网检索与下列条件匹配、将在指定日期于上海举办的具体线下活动公告和日程。"
        "查询条件中的日期是活动举办日期，不是网页发布日期；逐项查找具体活动名称。"
        "不要把仅因标题含有“未来城市”“未来科技”等字样的内容当作待举办活动。" + suffix
        + "优先返回主办方、赛事方、场馆、政府和授权票务页面；不要编造活动、日期、场馆或 URL。"
        + "events 数组只允许符合上海、日期和线下条件的活动；不符合条件的项目直接排除，不要作为解释项输出。"
        + "aliases 只填写搜索来源明确出现的官方外文名、简称或缩写；没有则输出空数组，禁止自行翻译或猜测。"
        + "只输出符合给定 JSON Schema 的数据。每个活动必须提供至少一个真实搜索来源序号；"
        + "source_indexes 只列明确支持该活动名称、举办日期和上海场馆的来源，不要列仅提供行业背景的网页；"
        + "没有来源、日期或场馆时使用 null，不得根据常识补写。影响判断必须明确是经营推断。"
        + "检索用于电竞酒店住宿需求研判的具体活动，优先关注跨城参赛、观赛、参展和多日行程。"
        + "普通旅游季、古镇游园、乡村市集、商场促销、社区文艺活动默认排除；"
        + "若有明确住宿关联依据，只返回对应的具体子活动。热门、大型、官方举办不构成住宿需求依据。"
        + "category 按活动实际内容填写 esports、sports、performance、acg、exhibition 或 other，不能直接继承查询分类。"
        + "汇总网页必须逐个活动对应名称、日期和场馆，禁止跨条目拼接字段。没有合格结果时返回空 events。"
    )
    hotel_context = qwen.get("hotel_context") or []
    if hotel_context:
        instructions += (
            "下面的所有门店共享本次检索，不要逐店重复联网搜索。hotel_relevance 对每家门店分别输出一项。"
            "reason 区分来源事实与经营推断，source_indexes 指向支持该活动事实的来源。"
            "已有场馆影响规则是门店关联上限；low 规则不能提升为 medium/high，未关联场馆或依据不足填 unknown。"
            "不得编造门店地址、距离、交通时间、外地人数或入住增量。整体 impact_level 不能替代逐店判断。"
            "门店与已有场馆规则：" + json.dumps(hotel_context, ensure_ascii=False, separators=(",", ":"))
        )
    else:
        instructions += "没有门店上下文时 hotel_relevance 输出空数组，不得猜测门店。"
    body = {
        "model": qwen["model"],
        "input": {
            "messages": [
                {
                    "role": "system",
                    "content": [{"text": "你是活动来源检索助手，只基于联网搜索结果回答。" + instructions}],
                },
                # Keep the user message as a compact search query. Qwen's
                # built-in search uses it to formulate retrieval terms.
                {"role": "user", "content": [{"text": query}]},
            ]
        },
        "parameters": {
            "enable_search": True,
            "enable_thinking": False,
            "incremental_output": True,
            "result_format": "message",
            "max_tokens": max(512, min(int(qwen.get("max_tokens", 4096)), 8192)),
            "response_format": _event_response_format(),
            "search_options": {
                "forced_search": True,
                "search_strategy": strategy,
                "enable_source": True,
                "enable_citation": True,
                "prepend_search_result": True,
            },
        },
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def search(
    config: dict[str, Any],
    query: str,
    limit: int,
    *,
    recent: bool = False,
    strategy: str | None = None,
    date_window: tuple[date, date] | None = None,
) -> list[dict[str, Any]]:
    """Return only source-backed structured events."""

    if not query.strip():
        return []
    qwen = _qwen_config(config)
    api_base = _api_base(config)
    api_key = _api_key(config)
    selected_strategy = str(strategy or qwen.get("search_strategy") or "turbo").strip().lower()
    if selected_strategy not in {"turbo", "max"}:
        raise QwenSearchError("Qwen 搜索策略仅支持 turbo 或 max")
    try:
        timeout_seconds = max(5, min(int(qwen.get("timeout_seconds", 45)), 90))
        retries = max(0, min(int(qwen.get("max_retries", 1)), 1))
    except (TypeError, ValueError) as exc:
        raise QwenSearchError("Qwen 搜索超时或重试配置无效") from exc

    request = Request(
        f"{api_base}/services/aigc/multimodal-generation/generation",
        data=_request_payload(query, qwen, recent=recent, strategy=selected_strategy),
        headers={
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "X-DashScope-SSE": "enable",
            "User-Agent": "hotel-demand-intelligence/1.0",
        },
        method="POST",
    )
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                sources, answer_text = _parse_sse_response(response, limit=max(limit * 3, limit))
                hotel_keys = {str(item["hotel_key"]) for item in (qwen.get("hotel_context") or [])}
                events = _structured_event_results(answer_text, sources, limit=limit,
                                                   hotel_keys=hotel_keys, date_window=date_window)
                if events is None:
                    if attempt < retries:
                        time.sleep(1)
                        continue
                    raise QwenSearchError("Qwen 联网搜索返回了无法解析的结构化响应")
                return events
        except HTTPError as exc:
            if exc.code == 401:
                raise QwenSearchError("Qwen 原生联网搜索认证失败") from exc
            if attempt < retries and (exc.code == 429 or 500 <= exc.code < 600):
                time.sleep(1)
                continue
            raise QwenSearchError(f"Qwen 原生联网搜索失败：HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            if attempt < retries:
                time.sleep(1)
                continue
            raise QwenSearchError(f"Qwen 原生联网搜索失败：{type(exc).__name__}") from exc
    raise QwenSearchError("Qwen 原生联网搜索失败")
