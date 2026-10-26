# Joyn Austria (`joyn`)

`JoynProvider` reads public catalogue metadata from Joyn **Austria** over the Joyn web GraphQL API
(`https://api.joyn.de/graphql`). It does not play or download video and needs no Joyn account. See
the [shared provider behavior](README.md) for everything it does *not* document here.

## Specialties and deviations

- **Path-based series identifier.** The configured `id` is a `joyn.at` series path/slug or a full
  URL; `_series_path()` strips the host or otherwise prefixes `/serien/`. The normalized series
  `external_id` is the `series.id` returned by the API, not the configured value (`joyn.py:609`,
  `joyn.py:277`).
- **Full GraphQL POST documents** (not persisted queries) with `operationName`/`variables`/`query`
  for the operations `EpisodeCalendarSeries`, `EpisodeCalendarSeason`, and `EpisodeCalendarJoynEpg`
  (`joyn.py:39`, `joyn.py:74`, `joyn.py:98`).
- **Required public web-client key and fixed headers.** `x-api-key` is taken from `JOYN_API_KEY`
  and raises when missing; requests also send `Joyn-Platform: web`,
  `Joyn-Client-Version: 5.1579.1`, `Joyn-Distribution-Tenant: JOYN_AT`, `Joyn-Country: AT`
  (`joyn.py:203`, `joyn.py:451`).
- **Own retry loop** instead of the shared `request_with_retries()`: retries `429`/`5xx` and
  request errors with exponential backoff + jitter, honouring `Retry-After` only when all-digit
  (`joyn.py:459`).
- **Response guards** require `data.page.__typename == "SeriesPage"`; GraphQL `errors[]` becomes
  `JoynGraphQLError` and any other structural surprise becomes `JoynMalformedResponseError`
  (`joyn.py:229`).
- **In-season pagination**: seasons are fetched in pages of 20 via `EpisodeCalendarSeason`, keyed
  by the season id, until `numberOfEpisodes` is reached; empty or over-fetched pages raise
  (`joyn.py:52`, `joyn.py:238`, `joyn.py:645`).
- **Release mapping**: `startsAt` (epoch or ISO) → `streaming` `release_at`; `airdate` is added as
  a separate `tv_broadcast` release only when it differs from `startsAt`, and is the fallback
  `streaming` date when `startsAt` is missing; `endsAt` → `available_until`; `path` (prefixed with
  `https://www.joyn.at`) → `url` (`joyn.py:529`, `joyn.py:546`).
- **Duration** comes from `episode.video.duration` (integer seconds, ISO/text strings handled too),
  normalized to whole minutes; the field is requested with `video { duration }` in both the series
  and season queries (`joyn.py:61`, `joyn.py:88`, `joyn.py:519`).
- **Preview means Joyn+.** Only a `PLUS`/`PREMIUM` badge marks a still subscriber-only episode as a
  preview (`joyn.py:515`, `joyn.py:576`, `joyn.py:127`): it counts while `startsAt <= airdate` —
  such drops store equality when the episode has no earlier linear slot (4 Elements S1E3/E4) — and
  while the linear airing is still ahead or the drop is at most 14 days old, so a season whose TV
  premiere is a month away keeps its prerelease, while the daily stream-after-air cadence of older
  subscriber seasons and badges Joyn never cleared stay regular. The `PREVIEW` marking is *not* a
  prerelease: it means "Vor TV", an episode that every logged-in Joyn account can already watch, so
  it never sets `preview`. Because that marking is authoritative, Joyn opts out of the importer's
  central `detect_previews` inference (`infers_catalog_previews = False`, `joyn.py:172`): a free
  early drop must not become a preview merely because it leads its linear airing by one to two
  weeks. The Kabel Eins airings are numberless `EpgEntry` rows that only carry the series title, so
  `merge_epg` attributes them to the running season's preview-only episodes in airing and episode
  order — 4 Elements S1E3/E4 each get their own 25 October airing as the regular date. The first
  airing after a preview drop is the episode's premiere, so a provider rerun flag on it is cleared
  as well.
- **Own EPG source**: `EpisodeCalendarJoynEpg` reads `liveStreams`/`epgEvents` day-by-day over a
  28-day look-ahead, skips blocks longer than 4h, and builds ids of the form
  `epg:<stream>:<program>:<epoch>` (`joyn.py:98`, `joyn.py:418`). Without an explicit page the
  endpoint stops after 20 events per stream and silently drops the rest of the window, so every day
  slice is read with `first`/`offset` pages of 200 until no stream reports a full page
  (`joyn.py:120`, `joyn.py:402`). EPG results are cached per `(endpoint, api_key, date)` across
  concurrent series imports (`joyn.py:363`).
- **EPG merge inputs**: `merge_epg(..., slots=derive_broadcast_slots(running_season, Europe/Vienna),
  timezone=Europe/Vienna, tolerance=2h, next_episode_external_id="epg:next:<season>:<n>")`. Only
  events matching the series title or an existing catalogue episode are considered, and events
  within the 15-minute broadcast tolerance of a catalogue release are dropped (`joyn.py:317`,
  `joyn.py:349`). EPG failures are logged and swallowed (`joyn.py:304`).
- **Undated catalogue episodes** emit the shared `first_seen_release()` placeholder directly
  (`joyn.py:564`); `description` is never populated.

The endpoint and headers were verified against the live Austria site on 2026-09-06. The API is
private and undocumented and may change without notice.
