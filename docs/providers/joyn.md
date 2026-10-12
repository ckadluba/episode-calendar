# Joyn Austria (`joyn`)

`JoynProvider` reads public catalogue metadata from Joyn **Austria** over the Joyn web GraphQL API
(`https://api.joyn.de/graphql`). It does not play or download video and needs no Joyn account. See
the [shared provider behavior](README.md) for everything it does *not* document here.

## Specialties and deviations

- **Path-based series identifier.** The configured `id` is a `joyn.at` series path/slug or a full
  URL; `_series_path()` strips the host or otherwise prefixes `/serien/`. The normalized series
  `external_id` is the `series.id` returned by the API, not the configured value (`joyn.py:207`,
  `joyn.py:552`).
- **Full GraphQL POST documents** (not persisted queries) with `operationName`/`variables`/`query`
  for the operations `EpisodeCalendarSeries`, `EpisodeCalendarSeason`, and `EpisodeCalendarJoynEpg`
  (`joyn.py:39`, `joyn.py:74`, `joyn.py:98`).
- **Required public web-client key and fixed headers.** `x-api-key` is taken from `JOYN_API_KEY`
  and raises when missing; requests also send `Joyn-Platform: web`,
  `Joyn-Client-Version: 5.1579.1`, `Joyn-Distribution-Tenant: JOYN_AT`, `Joyn-Country: AT`
  (`joyn.py:191`, `joyn.py:425`).
- **Own retry loop** instead of the shared `request_with_retries()`: retries `429`/`5xx` and
  request errors with exponential backoff + jitter, honouring `Retry-After` only when all-digit
  (`joyn.py:440`).
- **Response guards** require `data.page.__typename == "SeriesPage"`; GraphQL `errors[]` becomes
  `JoynGraphQLError` and any other structural surprise becomes `JoynMalformedResponseError`
  (`joyn.py:216`).
- **In-season pagination**: seasons are fetched in pages of 20 via `EpisodeCalendarSeason`, keyed
  by the season id, until `numberOfEpisodes` is reached; empty or over-fetched pages raise
  (`joyn.py:51`, `joyn.py:224`, `joyn.py:592`).
- **Release mapping**: `startsAt` (epoch or ISO) → `streaming` `release_at`; `airdate` is added as
  a separate `tv_broadcast` release only when it differs from `startsAt`, and is the fallback
  `streaming` date when `startsAt` is missing; `endsAt` → `available_until`; `path` (prefixed with
  `https://www.joyn.at`) → `url` (`joyn.py:504`, `joyn.py:521`).
- **Duration** comes from `episode.video.duration` (integer seconds, ISO/text strings handled too),
  normalized to whole minutes; the field is requested with `video { duration }` in both the series
  and season queries (`joyn.py:63`, `joyn.py:90`, `joyn.py:493`).
- **Conditional preview**: a `PREVIEW` marking only sets `preview` while `startsAt < airdate` (or a
  date is missing), so a regular release after an early drop is not marked a second time
  (`joyn.py:490`).
- **Own EPG source**: `EpisodeCalendarJoynEpg` reads `liveStreams`/`epgEvents` day-by-day over a
  28-day look-ahead, skips blocks longer than 4h, and builds ids of the form
  `epg:<stream>:<program>:<epoch>` (`joyn.py:98`, `joyn.py:387`). EPG results are cached per
  `(endpoint, api_key, date)` across concurrent series imports (`joyn.py:161`).
- **EPG merge inputs**: `merge_epg(..., slots=derive_broadcast_slots(running_season, Europe/Vienna),
  timezone=Europe/Vienna, tolerance=2h, next_episode_external_id="epg:next:<season>:<n>")`. Only
  events matching the series title or an existing catalogue episode are considered, and events
  within the 15-minute broadcast tolerance of a catalogue release are dropped (`joyn.py:304`,
  `joyn.py:336`). EPG failures are logged and swallowed (`joyn.py:290`).
- **Undated catalogue episodes** emit the shared `first_seen_release()` placeholder directly
  (`joyn.py:539`); `description` is never populated.

The endpoint and headers were verified against the live Austria site on 2026-09-06. The API is
private and undocumented and may change without notice.
