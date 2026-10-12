# STV Player (`stv`)

`STVProvider` scrapes the STV Player programme page for episode links and reads each episode from
the STV Player JSON API. The configured `id` is a composite `<programme slug>/<legacy id>`
(e.g. `big-brother/10a4928`). See the [shared provider behavior](README.md) for the baseline.

## Specialties and deviations

- **Two-phase fetch, no credentials.** The HTML page `{stv_player_url}/summary/{slug}` is scraped
  for `/episode/{4-char id}/{slug}` links (after unescaping `\/`, order-preserving dedup), then each
  episode is read from `{stv_api_url}/episodes/{short_id}` (`stv.py:88`, `stv.py:25`).
- **Composite identifier.** The `id` must contain exactly one `/` (rejecting `//` or `\`); only the
  first part is used as the programme slug, while the full input becomes the normalized series
  `external_id` (`stv.py:233`).
- **Special headers**: `Accept-Language: en-GB,en;q=0.9` and
  `User-Agent: episode-calendar/1.0`; the HTML request follows redirects, the API request does not
  (`stv.py:107`).
- **Season number** is parsed from `playerSeries.name` with `(?:series|season)\s+(\d+)`; no match
  yields `None`, rendered as the `Specials` season with id `"{external_id}:season:specials"`
  (`stv.py:255`, `stv.py:220`).
- **Episode number** must be a real int (`result.number`); numeric strings are rejected and the
  episode is skipped (`stv.py:181`). Episode/release ids are `result.guid`, falling back to the
  4-char short id (`stv.py:186`).
- **Duplicate `(season, number)` pairs collapse to the episode with the earliest `release_at`**
  (`stv.py:205`).
- **Release mapping**: `availability.from`, falling back to `schedule.startTime` → `streaming`
  `release_at`; `availability.until` → `available_until`; a missing start falls back to a
  `first_seen_release()` placeholder; url is `https://player.stv.tv/episode/{short_id}/{slug}`
  (`stv.py:179`, `stv.py:198`).
- Emits only `streaming` releases; never sets `tv_broadcast`, `preview`, or `rerun` (`stv.py:193`).
- **Uses the shared `request_with_retries()`**, issues all per-episode requests concurrently with
  `asyncio.gather` (no pagination), and applies only `filter_obsolete_seasons()` (`stv.py:109`,
  `stv.py:97`, `stv.py:103`).
