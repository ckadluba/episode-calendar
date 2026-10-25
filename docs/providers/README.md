# Provider adapters

Importer adapters live in `src/episode_calendar/importer/providers/`. Each one reads a public
provider source and returns the normalized data model; the import pipeline and the release policy
are shared. This page describes that shared behavior, so each provider page only has to document
its specialties and deviations from it.

## Shared behavior

- **Contract.** Every adapter implements `ProviderAdapter` (`providers/base.py`): a `slug` property
  and `async fetch_series(external_id) -> NormalizedSeries`. It returns the frozen normalized DTOs
  `NormalizedSeries -> NormalizedSeason -> NormalizedEpisode -> NormalizedEpisodeRelease`.
- **Releases.** `release_type` is `streaming` or `tv_broadcast`; `release_at` must be
  timezone-aware and is persisted in UTC. External IDs are provider-scoped and stable.
- **Duration.** `NormalizedEpisode.duration_minutes` is an optional whole-minute runtime stored on
  the episode (never on a release). Adapters normalize the provider value with the shared helpers
  below; providers without a runtime leave it `None`, and it never affects rerun/date policy.
- **Idempotent import.** Adapters only return data; `importer/service.py` upserts it. No adapter
  writes to the database, and no adapter is called from an API request.
- **Failure handling.** A failing series is isolated and logged, so sibling series of the same
  provider still import; the provider batch fails only when a series fails for a real reason. An
  HTTP `410 Gone` means the provider removed the series permanently (`is_resource_gone()` in
  `providers/http.py`): the importer logs a warning and skips it instead of failing the batch, and
  the entry should be dropped from `config/series.json`.
- **Catalogue is authoritative.** The provider catalogue is the base of every series tree. Linear
  EPG data is supplementary: it fills missing dates and announces episodes of a running season, but
  never overrides a real catalogue date (`providers/epg.py`).
- **Cross-cutting policy (owned by the importer, not the adapters).** `importer/service.py` and
  `providers/epg.py` implement rerun detection, preview detection for a catalogue drop that
  precedes the episode's own broadcast (`detect_previews`: one day to two weeks of lead, only on
  platforms whose `hasPrereleases` is `true`; a preview only announces an episode that has not
  aired yet, so once the broadcast is past its earlier drop is the regular release and the flag
  clears; the first airing after such a drop is the episode's premiere, so a provider rerun flag on
  it is cleared, and an on-demand drop inside that lead window never turns the episode's own
  broadcast into a rerun), first-seen placeholder preservation,
  preview gating via the platform `hasPrereleases` flag, obsolete-season cleanup, promotion of an
  EPG broadcast to the matching catalogue release at the same instant, and backfilling a catalogue
  URL onto a broadcast release. An EPG airing that cannot be attributed to an episode is never a
  rerun of an episode the catalogue still lists as subscriber-only: such airings premiere the
  running season's preview-only episodes in airing and episode order, and only the surplus airings
  stay reruns of the latest episode (`merge_epg()`).

## Shared helpers

- `base.py` — `first_seen_release()` (a `date_from_api=false`, `placeholder=true` streaming
  placeholder for a catalogue episode without a provider date), `episode_title_from_description()`,
  and the duration helpers `duration_minutes_from_seconds()` (half-up),
  `duration_minutes_from_iso8601()` (e.g. `PT1H4M59.136S`), and `duration_minutes_from_text()`
  (e.g. `1 hr 16 mins`, `1 Std. 26 Min.`).
- `http.py` — `request_with_retries()` (retries `429`/`5xx` and request errors, honours
  `Retry-After`) and `is_resource_gone()`, which reads an HTTP `410 Gone` out of a (possibly
  wrapped) provider error.
- `epg.py` — `apply_catalog_policy()` (`filter_obsolete_seasons()` + `ensure_catalog_dates()`),
  `derive_broadcast_slots()`, and `merge_epg()`.

## Configuration

Configured series live in `config/series.json` (override with `SERIES_CONFIG_PATH`). The
platform-level `hasPrereleases` flag (default `true`) declares whether the platform publishes early
streaming prereleases; on providers whose platform sets it to `false`, imported releases are never
marked as previews. On platforms that do publish them, an on-demand release that precedes the
episode's own linear broadcast by one day to two weeks is marked as a preview by the importer even
when the provider's catalogue carries no preview marking. The preview marks an announcement of an
episode that has not aired yet, so it is only set while that broadcast is still in the future; after
the premiere the earlier drop is the regular release and the flag is not set again.

## Providers

| Provider | Slug | Source | Series `id` | Credentials |
| --- | --- | --- | --- | --- |
| [Joyn Austria](joyn.md) | `joyn` | Joyn GraphQL | Series path/slug | `JOYN_API_KEY` |
| [RTL+](rtlplus.md) | `rtlplus` | RTL+ layout + guide TV | `<slug>-p_<program id>` | `RTLPLUS_OIDC_CLIENT_SECRET` |
| [BBC iPlayer](bbc_iplayer.md) | `bbc_iplayer` | BBC iPlayer Business Layer | Programme PID | – |
| [Channel 4](channel4.md) | `channel4` | channel4.com programme + TV guide | Brand slug | – |
| [ARD Mediathek](ardmediathek.md) | `ardmediathek` | ARD page-gateway + programme API | ARD element id | – |
| [STV Player](stv.md) | `stv` | player.stv.tv summary + JSON API | `<programme slug>/<legacy id>` | – |
| [Amazon Prime DE](amazon_prime_de.md) | `amazon_prime_de` | Prime Video detail page | Title ID (ASIN) | – |
| [Amazon Prime UK](amazon_prime_uk.md) | `amazon_prime_uk` | Prime Video detail page | Title ID (ASIN) | – |
