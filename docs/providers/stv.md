# STV Player (`stv`)

`STVProvider` scrapes the STV Player programme page for episode links and reads each episode from
the STV Player JSON API. The configured `id` is a composite `<programme slug>/<legacy id>`
(e.g. `im-a-celebrity-get-me-out-of-here/L2649`). See the [shared provider behavior](README.md) for
the baseline.

## Specialties and deviations

- **Two-phase fetch, no credentials.** The HTML page `{stv_player_url}/summary/{slug}` is scraped
  for `/episode/{4-char id}/{slug}` links (after unescaping `\/`, order-preserving dedup), then each
  episode is read from `{stv_api_url}/episodes/{short_id}` (`stv.py:127`, `stv.py:135`,
  `stv.py:37`).
- **Composite identifier.** The `id` must contain exactly one `/` (rejecting `//` or `\`); only the
  first part is used as the programme slug, while the full input becomes the normalized series
  `external_id` (`stv.py:396`).
- **Special headers**: `Accept-Language: en-GB,en;q=0.9` and
  `User-Agent: episode-calendar/1.0`; the HTML request follows redirects, the API request does not
  (`stv.py:146`, `stv.py:169`).
- **Season number** is parsed from `playerSeries.name` with `(?:series|season)\s+(\d+)`; no match
  yields `None`, rendered as the `Specials` season with id `"{external_id}:season:specials"`
  (`stv.py:417`, `stv.py:261`).
- **Episode number** must be a real int (`result.number`); numeric strings are rejected and the
  episode is skipped (`stv.py:215`). Episode/release ids are `result.guid`, falling back to the
  4-char short id (`stv.py:226`).
- **Duration** is the video's `_duration` text (e.g. `1 hr 16 mins`), normalized to whole minutes
  (`stv.py:230`).
- **Duplicate `(season, number)` pairs collapse to the episode with the earliest `release_at`**
  (`stv.py:246`).
- **Release mapping**: `availability.from`, falling back to `schedule.startTime` → `streaming`
  `release_at`; `availability.until` → `available_until`; a missing start falls back to a
  `first_seen_release()` placeholder; url is `https://player.stv.tv/episode/{short_id}/{slug}`
  (`stv.py:217`, `stv.py:236`).
- Catalogue episodes emit only `streaming` releases; the announced-season schedule (below) is the
  sole source of `tv_broadcast` releases (`stv.py:234`).
- **Announced season from the linear schedule.** STV's on-demand catalogue carries a season only
  once it is published, so an upcoming season first appears in the linear schedule
  (`GET {stv_api_url}/schedule?channel=stv&programme.guid={slug}&limit=100`) with an empty `episode`.
  The programme page's "new series" `promotionalSubtitle` is the signal to read that schedule
  (`stv.py:142`, `stv.py:275`, `stv.py:312`). Only entries on channel `stv` with `episode: null` and
  a start after the newest catalogued release count (`stv.py:346`); they are mapped to the shared
  `EpgBroadcast` and merged with `merge_epg()`, where the earliest airing declares
  `announces_season=next_season_number(series)` so the shared policy numbers the whole run in airing
  order (`stv.py:325`, `epg.py:297`). A `DEFAULT_NEW_SEASON_MIN_GAP` (90 days) guard suppresses a
  stale promo once the catalogue catches up, and the EPG stays supplementary: a failing schedule
  logs a warning and keeps the catalogue (`stv.py:320`, `stv.py:323`).
- **Retired programmes answer `410 Gone`.** STV permanently removes the summary page of a dropped
  programme (e.g. the retired `big-brother/10a4928`): the shared importer policy warns and skips the
  series instead of failing the provider import (see [shared provider behavior](README.md)).
- **Uses the shared `request_with_retries()`**, issues all per-episode requests concurrently with
  `asyncio.gather` (no pagination), and applies `filter_obsolete_seasons()` before the
  announced-season schedule merge (`stv.py:133`, `stv.py:139`).
