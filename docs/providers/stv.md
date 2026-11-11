# STV Player (`stv`)

`STVProvider` scrapes the STV Player programme page for episode links and reads each episode from
the STV Player JSON API. The configured `id` is a composite `<programme slug>/<legacy id>`
(e.g. `im-a-celebrity-get-me-out-of-here/L2649`). See the [shared provider behavior](README.md) for
the baseline.

## Specialties and deviations

- **Two-phase fetch, no credentials.** The HTML page `{stv_player_url}/summary/{slug}` is scraped
  for `/episode/{4-char id}/{slug}` links (after unescaping `\/`, order-preserving dedup), then each
  episode is read from `{stv_api_url}/episodes/{short_id}` (`stv.py:111`, `stv.py:32`).
- **Composite identifier.** The `id` must contain exactly one `/` (rejecting `//` or `\`); only the
  first part is used as the programme slug, while the full input becomes the normalized series
  `external_id` (`stv.py:357`).
- **Special headers**: `Accept-Language: en-GB,en;q=0.9` and
  `User-Agent: episode-calendar/1.0`; the HTML request follows redirects, the API request does not
  (`stv.py:133`).
- **Season number** is parsed from `playerSeries.name` with `(?:series|season)\s+(\d+)`; no match
  yields `None`, rendered as the `Specials` season with id `"{external_id}:season:specials"`
  (`stv.py:378`, `stv.py:249`).
- **Episode number** must be a real int (`result.number`); numeric strings are rejected and the
  episode is skipped (`stv.py:207`). Episode/release ids are `result.guid`, falling back to the
  4-char short id (`stv.py:213`).
- **Duration** is the video's `_duration` text (e.g. `1 hr 16 mins`), normalized to whole minutes
  (`stv.py:217`).
- **Duplicate `(season, number)` pairs collapse to the episode with the earliest `release_at`**
  (`stv.py:233`).
- **Release mapping**: `availability.from`, falling back to `schedule.startTime` → `streaming`
  `release_at`; `availability.until` → `available_until`; a missing start falls back to a
  `first_seen_release()` placeholder; url is `https://player.stv.tv/episode/{short_id}/{slug}`
  (`stv.py:204`, `stv.py:224`).
- Catalogue episodes emit only `streaming` releases; the synthesized announced premiere (below) is
  the sole `tv_broadcast` (`stv.py:221`, `stv.py:324`).
- **Announced next-season premiere.** STV promotes an upcoming season on the programme page before
  its episodes reach the on-demand catalogue (a "new series" `promotionalSubtitle` plus
  `scheduleNext`/`nextEpisode`). When that promo announces a start beyond every catalogued release
  and still in the future, the adapter synthesizes `S{n}E1` as a `tv_broadcast` release reusing the
  catalogue's own ids, so a later import promotes it in place instead of duplicating it
  (`stv.py:262`, `stv.py:295`).
- **Retired programmes answer `410 Gone`.** STV permanently removes the summary page of a dropped
  programme (e.g. the retired `big-brother/10a4928`): the shared importer policy warns and skips the
  series instead of failing the provider import (see [shared provider behavior](README.md)).
- **Uses the shared `request_with_retries()`**, issues all per-episode requests concurrently with
  `asyncio.gather` (no pagination), and applies `filter_obsolete_seasons()` before the announced
  premiere synthesis (`stv.py:135`, `stv.py:119`, `stv.py:125`).
