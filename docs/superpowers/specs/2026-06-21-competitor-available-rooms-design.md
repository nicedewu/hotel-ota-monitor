# Competitor available-room price payload

## Goal

Expose every explicitly available competitor room with a valid OTA price in the Agent-facing structured payload. Keep price comparisons based on the lowest available price for each comparable room type and stay date. Do not generate a human-readable brief or Excel output.

## Output contract

Each hotel object in `hermes_payload.json` gains an `available_rooms` array. It contains normalized room records only when all of the following are true:

- `status` is `success`.
- `available` is explicitly available or bookable.
- `price` is a valid OTA price.

Records that are unavailable, sold out, or have unknown availability are excluded. Existing `rooms` remains unchanged as the complete parsed source record list for compatibility.

`competitor_hotels[*].available_rooms` is the Agent input for displaying all currently available competitor room prices.

## Comparison

`price_compare` continues to compare matching `(stay_date, normalized_room_type)` keys. It selects the minimum valid price among each hotel's explicitly available rooms. The selected competitor room name and price identify the minimum-price source.

## Files

Keep only structured Agent outputs: `hermes_payload.json`, `price_compare.json`, `rooms_all.jsonl`, and `run_summary.json`. Do not write `agent_brief.md` or `ctrip_hotels.xlsx`, including removing stale copies if they exist.

## Tests

Add tests that prove:

1. Explicitly available rooms with valid prices appear in `available_rooms`.
2. Sold-out, unavailable, and availability-unknown rooms do not appear in `available_rooms`.
3. Multiple available rooms in the same normalized type compare using the lowest price.
4. The structured output list excludes the brief and Excel files.
