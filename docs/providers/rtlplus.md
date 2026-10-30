# RTL+ (`rtlplus`)

`RTLPlusProvider` reads the RTL+ web layout API (`plus.rtl.de`) together with RTL+'s own linear EPG
(`guidetv`). The configured `id` is a slug ending in the numeric program id
(`<slug>-p_<program id>`, e.g. `adam-sucht-eva-p_7233`). See the
[shared provider behavior](README.md) for the baseline.

## Specialties and deviations

- **Two-step authentication.** An OIDC `client_credentials` token (client id `anonymous-user`,
  secret from `RTLPLUS_OIDC_CLIENT_SECRET`) is followed by a device/auth request that returns a
  Bedrock token. Layout calls send the Bedrock token plus customer/release headers; a `401`
  triggers a single re-authentication (`rtlplus.py:541`, `rtlplus.py:560`, `rtlplus.py:826`,
  `rtlplus.py:838`).
- **Own retry loop** instead of the shared helper; only an all-digit `Retry-After` is honoured
  (`rtlplus.py:820`).
- **Slug in, numeric id out.** The program id is parsed with `(?:^|[_-])p_(\d+)$` and must be
  numeric; the normalized series `external_id` is the bare numeric program id, while the public
  series URL uses the configured slug (`rtlplus.py:173`, `rtlplus.py:1180`).
- **Season block shape.** Seasons are the layout blocks whose `analytics.tealium.from` is
  `feature.videos_by_season_by_program`; season/episode numbers come from `Staffel N`/`Folge N` in
  the item highlight. Items with `clip_type` not in `(None, "vi")` are skipped, and duplicates
  within a season keep the first occurrence (`rtlplus.py:905`, `rtlplus.py:1058`, `rtlplus.py:1130`).
- **Synthetic IDs.** Season `"{program_id}:season:{n}"`, announced undated next episode
  `"{program_id}:season:{n}:episode:{next}"`, EPG event `"epg:{code}:{epoch}"`, and merge-created
  episode `"epg:next:{season_id}:{number}"` (`rtlplus.py:1180`, `rtlplus.py:1146`, `rtlplus.py:441`,
  `rtlplus.py:258`).
- **Per-episode runtime and upload date from the public page.** The layout API exposes neither, so
  each episode's runtime and original streaming date are read from the schema.org `duration`
  (e.g. `PT51M56S` → 52 min) and `uploadDate` on its public page; episodes without a public page
  (such as the synthesized next episode) keep `duration_minutes=None` and fall back to other date
  sources (`rtlplus.py:609`, `rtlplus.py:648`, `rtlplus.py:782`).
- **Current-season detection** uses the maximum season number seen across season blocks, item
  highlights, and a weekday-programmation block; the same weekday block also yields the page-level
  diffusion episode number (`rtlplus.py:1016`, `rtlplus.py:1032`).
- **Derived number for an unnumbered "Neue Staffel" announcement.** When RTL+ introduces the
  upcoming season in a season block titled only "Neue Staffel ..." (no number) whose items carry no
  `Staffel N` label — for example `"Bonus-Inhalte 2026 • Folge 1"` — the new season number is derived
  as the highest known season plus one. It is only adopted when the block yields a datable premiere
  for episode 1, and the announced premiere `S{n+1} E1` is synthesized so the finished previous
  season is no longer dated with the upcoming cadence (`rtlplus.py:869`, `rtlplus.py:1160`).
- **Derived "premiere anchor" dates.** For a non-current season, dates are derived as
  `anchor + weeks * episode_number` with `date_from_api=false` — a *derived but real* date, not a
  placeholder. Anchors come from the JSON-LD `uploadDate` of the "Auftakt von Staffel N" preview
  clip; a missing preview is warned and skipped (`rtlplus.py:1089`, `rtlplus.py:619`).
- **Completed-season dates from each episode's `uploadDate`.** A finished season no longer carries
  dates in its catalogue block, but every episode page still publishes the original streaming date.
  When the catalogue, the diffusion episode, and a premiere anchor all yield no date, that
  `uploadDate` is used (`date_from_api=false`, not a placeholder) so the season keeps its real dates
  instead of being dated with the upcoming season's cadence (`rtlplus.py:1097`).
- **Page-level diffusion fallback is a derived date.** When the catalogue schedule and the weekday
  block both yield no date, the page-level `diffusionDate` dates every undated episode of the running
  season. It identifies the *newest streaming batch*, not the individual episode, so it is emitted
  with `date_from_api=false`. The importer uses a derived date only to fill a gap and never to
  re-date an episode that already has a stored date, so a weekly show keeps the date of its first
  batch instead of moving forward with every new one (`rtlplus.py:1099`).
- **Streaming-only adapter**: it emits only `streaming` releases; every `tv_broadcast` release comes
  from the EPG merge (`rtlplus.py:1110`). Its SEO markdown `_schedule()` parser understands three
  layouts (bold `Folge N` lines, a date table, and an `ab/startet` line expanded to 52 weekly
  episodes).
- **Inline `preview` marking** (`_mark_previews`): flags same-day streaming drops that precede the
  episode's earliest linear premiere, and the catalogue episode matched to a premiere airing within
  the 14-day look-ahead (`rtlplus.py:286`). The importer's central preview policy `detect_previews`
  additionally flags a broadcast-anchored drop of one day to two weeks as a preview, so a guide
  premiere the inline marking missed still yields a preview. A preview announces an episode that has
  not aired yet, so once the linear premiere is in the past the drops in front of it are treated as
  the regular releases they already were (`rtlplus.py:266`).
- **Own EPG source and slots.** The `guidetv` guide is fetched from `now - lookback` (default 21
  days) to `now + 14 days`, paginated, deduped, blocks longer than 4h dropped, and cached per
  process (`rtlplus.py:371`). `merge_epg` receives slots derived from the EPG's own premieres
  (falling back to catalogue slots), `timezone=Europe/Vienna`, and `tolerance=15min`; broadcasts
  carry no season number, so they resolve against the running season (`rtlplus.py:332`,
  `rtlplus.py:251`).
- **Undated episodes** use `first_seen_release()` placeholders (built from `datetime.now(UTC)`
  directly); the synthetic next episode is added only when its derived date is today or later in
  `Europe/Vienna` (`rtlplus.py:1106`, `rtlplus.py:1133`).
