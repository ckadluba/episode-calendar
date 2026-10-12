# BBC iPlayer (`bbc_iplayer`)

`BBCIPlayerProvider` reads public programme metadata from the BBC iPlayer Business Layer (IBL) API.
The configured `id` is a BBC programme PID (e.g. `m002csng`). See the
[shared provider behavior](README.md) for the baseline.

## Specialties and deviations

- **Public IBL API, no credentials.** Programme `/programmes/{pid}`, the paginated catalogue
  `/programmes/{pid}/episodes` (`per_page=100`), and the channel schedule
  `/channels/{channel}/schedule/{YYYY-MM-DD}` (`bbc_iplayer.py:98`, `bbc_iplayer.py:108`,
  `bbc_iplayer.py:156`).
- **Own pagination/retry** instead of the shared helpers: a page whose number does not match the
  request, or an empty page before the total, raises (`bbc_iplayer.py:120`, `bbc_iplayer.py:186`).
- **Seasons only via subtitle.** The season number is parsed from the episode subtitle with
  `(?:series|cyfres)\s+(\d+)`; a subtitle without it becomes the `Specials` season. Season ids are
  synthesized as `"{pid}:season:{n|specials}"` and specials sort last (`bbc_iplayer.py:335`,
  `bbc_iplayer.py:239`).
- **Episode numbers** also come from the subtitle (`Series N: [Episode ]M`); a bare `Episode N`
  without a `Series N:` prefix yields no number (`bbc_iplayer.py:340`).
- **Release mapping, in priority order** (`bbc_iplayer.py:299`): a schedule-injected planned start;
  otherwise the `original` version's availability start/end; otherwise the episode's
  `release_date_time`. No release is produced if none of these exist.
- **Planned future episodes are emitted as `streaming` releases** (not `tv_broadcast`) from the
  channel schedule with `date_from_api` left `true`; already-known catalogue episodes are not
  duplicated (`bbc_iplayer.py:167`, `bbc_iplayer.py:281`).
- **Schedule window** starts today (no look-back) and runs `BBC_IPLAYER_SCHEDULE_DAYS` (default 14)
  over `BBC_IPLAYER_SCHEDULE_CHANNELS` (default `bbc_one_london`); per-day payloads are cached and
  `404`s tolerated (`bbc_iplayer.py:149`, `bbc_iplayer.py:160`).
- **Placeholder path**: an episode with a number but no release gets `first_seen_release()`
  (`date_from_api=false`, `placeholder=true`); an episode with neither a release nor a number is
  dropped (`bbc_iplayer.py:288`, `bbc_iplayer.py:264`).
- **URL** is always synthesized as `https://www.bbc.co.uk/iplayer/episode/{id}`
  (`bbc_iplayer.py:276`).
- Never sets `preview`, `rerun`, or a derived (non-placeholder) `date_from_api=false` date. It only
  calls the shared `filter_obsolete_seasons()` and does not use `merge_epg()` (`bbc_iplayer.py:142`).
