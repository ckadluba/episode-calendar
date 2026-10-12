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
- **Idempotent import.** Adapters only return data; `importer/service.py` upserts it. No adapter
  writes to the database, and no adapter is called from an API request.
- **Catalogue is authoritative.** The provider catalogue is the base of every series tree. Linear
  EPG data is supplementary: it fills missing dates and announces episodes of a running season, but
  never overrides a real catalogue date (`providers/epg.py`).
- **Cross-cutting policy (owned by the importer, not the adapters).** `importer/service.py` and
  `providers/epg.py` implement rerun detection, first-seen placeholder preservation, preview gating
  via the platform `hasPrereleases` flag, obsolete-season cleanup, promotion of an EPG broadcast to
  the matching catalogue release at the same instant, and backfilling a catalogue URL onto a
  broadcast release.

## Shared helpers

- `base.py` — `first_seen_release()` (a `date_from_api=false`, `placeholder=true` streaming
  placeholder for a catalogue episode without a provider date) and `episode_title_from_description()`.
- `http.py` — `request_with_retries()` (retries `429`/`5xx` and request errors, honours
  `Retry-After`).
- `epg.py` — `apply_catalog_policy()` (`filter_obsolete_seasons()` + `ensure_catalog_dates()`),
  `derive_broadcast_slots()`, and `merge_epg()`.

## Configuration

Configured series live in `config/series.json` (override with `SERIES_CONFIG_PATH`). The
platform-level `hasPrereleases` flag (default `true`) declares whether the platform publishes early
streaming prereleases; on providers whose platform sets it to `false`, imported releases are never
marked as previews.

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
