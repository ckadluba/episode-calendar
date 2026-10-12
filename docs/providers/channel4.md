# Channel 4 (`channel4`)

`Channel4Provider` reads the public channel4.com programme JSON plus the channel4.com TV guide. The
configured `id` is the brand web-safe slug (e.g. `taskmaster`). See the
[shared provider behavior](README.md) for the baseline.

## Specialties and deviations

- **Two sources, no credentials.** Catalogue `GET {base_url}/{slug}?json=true` and TV guide
  `GET {tv_guide_url}/{YYYY-MM-DD}` (`channel4.py:126`, `channel4.py:303`).
- **No retries**: a direct `client.get`; only a `404` on a guide day is tolerated
  (`channel4.py:306`).
- **The TV-guide schedule is adapter-local**, fetched per day for `channel4_schedule_days` (default
  14) starting today in `Europe/London`, parsed from the embedded `<script id="initialData">` JSON,
  and reading only the `C4` channel. Responses are cached per instance (`channel4.py:246`,
  `channel4.py:313`).
- **Numbering** comes from the JSON `seriesNumber`/`episodeNumber` ints; entries where either is not
  an int are skipped (no specials/season-0 handling). Season ids are `"{slug}:series:{n}"`, sorted
  ascending (`channel4.py:164`, `channel4.py:209`).
- **Episode id is the `programmeId`** (required, else malformed) and is reused as the streaming
  release `external_id` (`channel4.py:166`, `channel4.py:236`).
- **Catalogue date** is parsed from the HTML-ish `dateLabel` ("First shown: ...") and set to
  **midnight `Europe/London`**. A missing or unparseable `dateLabel` falls back to a
  `first_seen_release()` placeholder (`channel4.py:222`, `channel4.py:173`).
- **Inline broadcast merge keyed on `programmeId`.** TV-guide programmes with
  `brandWebSafeTitle == slug` and `isRepeat == false` are matched by `programmeId`; unmatched ones
  are injected as new episodes whose season/episode come from the summary `(S.. Ep..)`. A matched
  programme adds a `tv_broadcast` release (start from `startDate`, url prefixed with
  `https://www.channel4.com`) (`channel4.py:175`, `channel4.py:257`, `channel4.py:277`). Broadcast
  releases are keyed by `external_id` with `None` for broadcasts, so only the last scheduled airing
  per episode is kept (`channel4.py:172`).
- Only calls the shared `filter_obsolete_seasons()`; it does not use `merge_epg()` and never sets
  `preview`, `rerun`, or `available_until` (`channel4.py:137`).
