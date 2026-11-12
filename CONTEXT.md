# OTA Price Monitoring

This context covers hotel operational queries and OTA price comparisons used by the hotel operator.

## Language

**Cached Price Query**:
A request that returns the latest successful OTA price comparison without starting a new capture.
_Avoid_: Refresh, live scrape

**Forced Price Refresh**:
A user-requested capture for exactly one requested stay date that persists the new result before returning it.
_Avoid_: Cached query, scheduled snapshot

**Full Price Comparison**:
Every valid same-date, same-room-type comparison produced for the requested stay date, without truncating rows or fields.
_Avoid_: Brief, top prices, compact comparison

**Scheduled Business Snapshot**:
The timer-driven operating report workflow, independent from interactive cached queries and forced refreshes.
_Avoid_: Forced refresh, interactive query

**Pricing Lesson**:
A reviewed hotel-specific rule or conclusion stored in Mnemosyne and used as experience, never as a replacement for TimescaleDB facts.
_Avoid_: Raw price history, unreviewed log

**Historical Similar Case**:
A prior same-room-type decision ranked by weekday, event demand, price gap, and final hotel-level sales facts from TimescaleDB.
_Avoid_: Causal proof, room-level sales result

**Pricing Review Draft**:
A non-persisted lesson proposal generated from actual hotel-level sales results and prior room-type decisions; it enters Mnemosyne only through explicit `memory-add-pricing-lesson`.
_Avoid_: Automatic lesson, automatic memory write

**Hindsight Weekly Review**:
A hotel-scoped weekly operating review that reads TimescaleDB/PostgreSQL facts first, generates per-`hotel_key` reports and memory candidates, and may stage or explicitly write reviewed lessons.
_Avoid_: Daily price decision, output/latest source of truth, cross-hotel single-store report

**Weekly Review Admin Overview**:
A multi-hotel management summary that may compare stores at an aggregate level; it is not sent to branch webhooks.
_Avoid_: Single-hotel report, branch distribution
