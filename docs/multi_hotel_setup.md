# Multi Hotel Setup

## hotel_key rules

- Own hotels use `own_<area_or_store>`, for example `own_demo_a`.
- Competitors use `competitor_<area>_<number>`, for example `competitor_demo_a_02`.
- The SQL example seeds disabled demo hotels. Configure and activate your own hotel and competitor rows before scheduling runs.

## PMS env

Create `~/.hotel_agent/pms_multi.env` with names only in this shape:

```env
PMS_BASE_URL=
PMS_OWN_DEMO_A_HOTEL_CODE=
PMS_OWN_DEMO_A_PARTNER_CODE=
PMS_OWN_DEMO_A_CLIENT_SECRET=
```

Per-hotel base URL is optional:

```env
PMS_OWN_DEMO_A_BASE_URL=
```

`--hotel-key own_demo_f` maps to prefix `OWN_DEMO_F`.

## Feishu webhook env

`hotel_push_routes.webhook_env_key` stores only an env var name. The real webhook value lives in `~/.hotel_agent/webhook.env`.

## Add an own hotel

1. Insert one `hotels` row with `hotel_role='own'`, `hotel_key`, `hotel_name`, `ota_url`, and `is_active`.
2. Insert competitor bindings in `hotel_competitors`.
3. Insert optional room rules in `room_type_mapping`.
4. Insert push routes in `hotel_push_routes`.
5. Add PMS variables in `pms_multi.env`.

## Add a competitor

1. Insert one `hotels` row with `hotel_role='competitor'`.
2. Bind it through `hotel_competitors`.
3. Add optional hotel-specific room mapping rows.

## Configure venue rules

Use `hotel_venue_impact_rules` to set the impact of a venue for a configured hotel. Set `own_hotel_key` to a key in `hotels` and `venue` to the corresponding name in `exhibition_events`. Run `db/hotel_event_impact_venue_rules.sql` after importing events if you need to populate the legacy `hotel_event_impact` table.

## Deployment templates

`deploy/systemd/` contains sample units for the `ctrip-monitor` service account, with `/var/lib/ctrip-monitor` as its home and `/opt/hotel-ota-monitor` as the installation directory. Set these locations and the timer schedules for your environment before enabling the units. Set `PMS_AGENT_DIR` and, if needed, `PMS_PYTHON` for commands that call the PMS integration.

## Verify

```bash
python3 main.py scheduled-snapshot --own-hotel-key own_demo_a --dry-run
python3 main.py scheduled-snapshot --all-own-hotels --stagger-seconds 1 --dry-run
python3 main.py business-snapshot-report --own-hotel-key own_demo_a --date "$(date +%F)" --days 1 --detail-level full
```

Dry-run does not call PMS, scrape OTA, write DB, write price decisions, or push Feishu.

## Enable

1. Apply the additive migration manually after review.
2. Fill `pms_multi.env`.
3. Fill OTA URLs and competitor bindings.
4. Fill webhook env values.
5. Run dry-run.
6. Run a single-hotel check with your configured key before enabling timers.

## Rollback

- Do not drop tables.
- Disable new routes or hotel rows by setting `is_active=false`.
- Revert code deployment if needed.
- Keep generated migration unapplied if validation fails.

## PMS price ban

PMS is inventory and operating-status only. Room price facts must come from OTA data.
