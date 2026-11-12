from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HotelConfig:
    key: str
    role: str
    name: str
    url: str
    output: str
    competitors: tuple[str, ...] = ()
    feishu_webhook_env_key: str = ""


def load_hotels(config_path: str | Path) -> list[HotelConfig]:
    path = Path(config_path)
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and ("own_hotels" in payload or "competitor_hotels" in payload):
        hotels = [*payload.get("own_hotels", []), *payload.get("competitor_hotels", [])]
    else:
        hotels = payload.get("hotels", payload if isinstance(payload, list) else [])
    if not isinstance(hotels, list):
        raise ValueError(f"{path} 中的 hotels 必须是列表")

    result: list[HotelConfig] = []
    for item in hotels:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        if not key:
            raise ValueError(f"{path} 中存在缺少 key 的酒店配置: {item}")
        role = str(item.get("role", "unknown")).strip() or "unknown"
        if role not in {"own", "competitor", "unknown"}:
            role = "unknown"
        name = str(item.get("name", "")).strip() or key
        url = str(item.get("ota_url") or item.get("url") or "").strip()
        output = str(item.get("output", f"output/{key}")).strip()
        competitors = tuple(str(value).strip() for value in item.get("competitors", []) if str(value).strip())
        webhook_env_key = str(item.get("feishu_webhook_env_key", "")).strip()
        result.append(
            HotelConfig(
                key=key,
                role=role,
                name=name,
                url=url,
                output=output,
                competitors=competitors,
                feishu_webhook_env_key=webhook_env_key,
            )
        )
    return result


def find_hotel(config_path: str | Path, hotel_key: str) -> HotelConfig:
    hotels = load_hotels(config_path)
    for hotel in hotels:
        if hotel.key == hotel_key:
            return hotel
    available = ", ".join(hotel.key for hotel in hotels) or "无"
    raise ValueError(f"未找到 hotel-key={hotel_key}，当前可用 key: {available}")


def first_hotel_url(config_path: str | Path) -> str | None:
    hotels = load_hotels(config_path)
    return next((hotel.url for hotel in hotels if hotel.url), None)
