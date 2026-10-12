# RTL+ (`rtlplus`)

`RTLPlusProvider` reads the RTL+ web layout API (`plus.rtl.de`) together with RTL+'s own linear EPG
(`guidetv`). The configured `id` is a slug ending in the numeric program id
(`<slug>-p_<program id>`, e.g. `adam-sucht-eva-p_7233`). See the
[shared provider behavior](README.md) for the baseline.

## Specialties and deviations

- **Two-step authentication.** An OIDC `client_credentials` token (client id `anonymous-user`,
  secret from `RTLPLUS_OIDC_CLIENT_SECRET`) is followed by a device/auth request that returns a
  Bedrock token. Layout calls send the Bedrock token plus customer/release headers; a `401`
  triggers a single re-authentication (`rtlplus.py:536`, `rtlplus.py:555`, `rtlplus.py:727`).
- **Own retry loop** instead of the shared helper; only an all-digit `Retry-After` is honoured
  (`rtlplus.py:720`).
- **Slug in, numeric id out.** The program id is parsed with `(?:^|[_-])p_(\d+)$` and must be
  numeric; the normalized series `external_id` is the bare numeric program id, while the public
  series URL uses the configured slug (`rtlplus.py:168`, `rtlplus.py:993`).
- **Season block shape.** Seasons are the layout blocks whose `analytics.tealium.from` is
  `feature.videos_by_season_by_program`; season/episode numbers come from `Staffel N`/`Folge N` in
  the item highlight. Items with `clip_type` not in `(None, "vi")` are skipped, and duplicates
  within a season keep the first occurrence (`rtlplus.py:779`, `rtlplus.py:894`, `rtlplus.py:953`).
- **Synthetic IDs.** Season `"{program_id}:season:{n}"`, announced undated next episode
  `"{program_id}:season:{n}:episode:{next}"`, EPG event `"epg:{code}:{epoch}"`, and merge-created
  episode `"epg:next:{season_id}:{number}"` (`rtlplus.py:986`, `rtlplus.py:436`, `rtlplus.py:253`).
- **Current-season detection** uses the maximum season number seen across season blocks, item
  highlights, and a weekday-programmation block; the same weekday block also yields the page-level
  diffusion episode number (`rtlplus.py:820`, `rtlplus.py:1012`).
- **Derived "premiere anchor" dates.** For a non-current season, dates are derived as
  `anchor + weeks * episode_number` with `date_from_api=false` — a *derived but real* date, not a
  placeholder. Anchors come from the JSON-LD `uploadDate` of the "Auftakt von Staffel N" preview
  clip; a missing preview is warned and skipped (`rtlplus.py:922`, `rtlplus.py:608`).
- **Streaming-only adapter**: it emits only `streaming` releases; every `tv_broadcast` release comes
  from the EPG merge (`rtlplus.py:904`). Its SEO markdown `_schedule()` parser understands three
  layouts (bold `Folge N` lines, a date table, and an `ab/startet` line expanded to 52 weekly
  episodes).
- **Inline `preview` marking** (`_mark_previews`): flags same-day streaming drops that precede the
  episode's earliest linear premiere, and the catalogue episode matched to a premiere airing within
  the 14-day look-ahead (`rtlplus.py:278`).
- **Own EPG source and slots.** The `guidetv` guide is fetched from `now - lookback` (default 21
  days) to `now + 14 days`, paginated, deduped, blocks longer than 4h dropped, and cached per
  process (`rtlplus.py:366`). `merge_epg` receives slots derived from the EPG's own premieres
  (falling back to catalogue slots), `timezone=Europe/Vienna`, and `tolerance=15min`; broadcasts
  carry no season number, so they resolve against the running season (`rtlplus.py:327`,
  `rtlplus.py:246`).
- **Undated episodes** use `first_seen_release()` placeholders (built from `datetime.now(UTC)`
  directly); the synthetic next episode is added only when its derived date is today or later in
  `Europe/Vienna` (`rtlplus.py:930`, `rtlplus.py:957`).
