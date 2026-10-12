# RTL+ (`rtlplus`)

`RTLPlusProvider` reads the RTL+ web layout API (`plus.rtl.de`) together with RTL+'s own linear EPG
(`guidetv`). The configured `id` is a slug ending in the numeric program id
(`<slug>-p_<program id>`, e.g. `adam-sucht-eva-p_7233`). See the
[shared provider behavior](README.md) for the baseline.

## Specialties and deviations

- **Two-step authentication.** An OIDC `client_credentials` token (client id `anonymous-user`,
  secret from `RTLPLUS_OIDC_CLIENT_SECRET`) is followed by a device/auth request that returns a
  Bedrock token. Layout calls send the Bedrock token plus customer/release headers; a `401`
  triggers a single re-authentication (`rtlplus.py:537`, `rtlplus.py:556`, `rtlplus.py:810`).
- **Own retry loop** instead of the shared helper; only an all-digit `Retry-After` is honoured
  (`rtlplus.py:803`).
- **Slug in, numeric id out.** The program id is parsed with `(?:^|[_-])p_(\d+)$` and must be
  numeric; the normalized series `external_id` is the bare numeric program id, while the public
  series URL uses the configured slug (`rtlplus.py:169`, `rtlplus.py:1078`).
- **Season block shape.** Seasons are the layout blocks whose `analytics.tealium.from` is
  `feature.videos_by_season_by_program`; season/episode numbers come from `Staffel N`/`Folge N` in
  the item highlight. Items with `clip_type` not in `(None, "vi")` are skipped, and duplicates
  within a season keep the first occurrence (`rtlplus.py:863`, `rtlplus.py:978`, `rtlplus.py:1038`).
- **Synthetic IDs.** Season `"{program_id}:season:{n}"`, announced undated next episode
  `"{program_id}:season:{n}:episode:{next}"`, EPG event `"epg:{code}:{epoch}"`, and merge-created
  episode `"epg:next:{season_id}:{number}"` (`rtlplus.py:1071`, `rtlplus.py:437`, `rtlplus.py:254`).
- **Per-episode runtime from the public page.** The layout API exposes no duration, so each episode's
  runtime is read from the schema.org `duration` on its public page (e.g. `PT51M56S` → 52 min) and
  episodes without a public page (such as the synthesized next episode) keep `duration_minutes=None`
  (`rtlplus.py:605`, `rtlplus.py:643`, `rtlplus.py:766`).
- **Current-season detection** uses the maximum season number seen across season blocks, item
  highlights, and a weekday-programmation block; the same weekday block also yields the page-level
  diffusion episode number (`rtlplus.py:904`, `rtlplus.py:1097`).
- **Derived "premiere anchor" dates.** For a non-current season, dates are derived as
  `anchor + weeks * episode_number` with `date_from_api=false` — a *derived but real* date, not a
  placeholder. Anchors come from the JSON-LD `uploadDate` of the "Auftakt von Staffel N" preview
  clip; a missing preview is warned and skipped (`rtlplus.py:1006`, `rtlplus.py:614`).
- **Streaming-only adapter**: it emits only `streaming` releases; every `tv_broadcast` release comes
  from the EPG merge (`rtlplus.py:988`). Its SEO markdown `_schedule()` parser understands three
  layouts (bold `Folge N` lines, a date table, and an `ab/startet` line expanded to 52 weekly
  episodes).
- **Inline `preview` marking** (`_mark_previews`): flags same-day streaming drops that precede the
  episode's earliest linear premiere, and the catalogue episode matched to a premiere airing within
  the 14-day look-ahead (`rtlplus.py:279`).
- **Own EPG source and slots.** The `guidetv` guide is fetched from `now - lookback` (default 21
  days) to `now + 14 days`, paginated, deduped, blocks longer than 4h dropped, and cached per
  process (`rtlplus.py:367`). `merge_epg` receives slots derived from the EPG's own premieres
  (falling back to catalogue slots), `timezone=Europe/Vienna`, and `tolerance=15min`; broadcasts
  carry no season number, so they resolve against the running season (`rtlplus.py:328`,
  `rtlplus.py:247`).
- **Undated episodes** use `first_seen_release()` placeholders (built from `datetime.now(UTC)`
  directly); the synthetic next episode is added only when its derived date is today or later in
  `Europe/Vienna` (`rtlplus.py:1014`, `rtlplus.py:1042`).
