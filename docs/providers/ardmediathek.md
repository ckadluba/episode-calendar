# ARD Mediathek (`ardmediathek`)

`ARDMediathekProvider` reads the public ARD page-gateway widget API plus the ARD programme API. The
configured `id` is the ARD element id (the long `Y3JpZDov...` base64 value), used verbatim. See the
[shared provider behavior](README.md) for the baseline.

## Specialties and deviations

- **REST, not GraphQL; no credentials.** Catalogue `GET {ardmediathek_api_url}/{asset_id}` and
  programme `GET {ardmediathek_program_url}?day=` (`ardmediathek.py:104`, `ardmediathek.py:182`);
  episode descriptions come from each teaser's own `links.target.href`.
- **No retries**: a raw `client.get` with error wrapping only (`ardmediathek.py:104`).
- **Manual pagination** over `pageNumber`/`pageSize` (default 100) until
  `pagination.totalElements` is reached; an empty page before the total raises
  (`ardmediathek.py:126`).
- **Numbering parsed from the title** `(S<season>/E<episode>)` at the end of `longTitle`/
  `mediumTitle`. WDR-style titles without that suffix (`Folge N: Title`) are accepted only when
  `show.availableSeasons` names exactly one season; ambiguous or unnumbered teasers are skipped,
  so there is no specials handling. Episode titles strip the `Folge N:` prefix and the `(Sx/Ey)`
  suffix (`ardmediathek.py:556`, `ardmediathek.py:559`, `ardmediathek.py:535`).
- **No placeholders.** Every catalogue episode carries a real date, so the adapter never emits
  `placeholder`/`date_from_api=false` and never uses `ensure_catalog_dates()`; it only calls
  `filter_obsolete_seasons()` (`ardmediathek.py:149`).
- **Two dates per episode, detail wins.** The teaser `broadcastedOn` is the initial `streaming`
  date, then the episode-detail `broadcastedOnDateTime` (the actual on-demand publication)
  overwrites it; `availableTo` → `available_until`; url is
  `https://www.ardmediathek.de/video/{episode_id}` (`ardmediathek.py:596`, `ardmediathek.py:309`).
- **Duration** is the teaser's `duration` (integer seconds), normalized to whole minutes
  (`ardmediathek.py:589`); episodes without the field stay `None`.
- **Provider-specific EPG linking.** Programme airings are matched to episodes by target `urlId`,
  then by exact title, then by normalized synopsis; a title matching several episodes yields one
  broadcast per match. Programme/teaser detail fetches run in unbounded parallel `asyncio.gather`
  (`ardmediathek.py:270`).
- **Schedule window** runs from `today - lookback` (default 21) to `today + schedule_days` (default
  14) day by day, tolerating a `404` per day (`ardmediathek.py:178`).
- **Calls shared `merge_epg()`** with `timezone=Europe/Berlin`, `tolerance=15min`, and the
  `scheduled:{season}:{number}` episode-id factory; `slots` is omitted, so they are derived from the
  running season. Broadcasts carry only `linked_episode_external_id` (no season/episode number)
  (`ardmediathek.py:469`, `ardmediathek.py:466`).
- Never returns a series-level `description` (`ardmediathek.py:606`).
