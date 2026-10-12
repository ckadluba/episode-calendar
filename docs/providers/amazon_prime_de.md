# Amazon Prime DE (`amazon_prime_de`)

`AmazonPrimeDEProvider` reads the JSON detail data used by the public German Prime Video web app at
`GET {base_url}/gp/video/api/getDetailPage`. The configured `id` is the Prime Video title ID (an
ASIN or `amzn1.dv.gti...`). See the [shared provider behavior](README.md) for the baseline.

## Specialties and deviations

- **Single request, no credentials.** One `getDetailPage` call with `titleID`, `isElcano=1`, and
  `sections=Atf`/`Btf`, plus special `Accept-Language: de-DE,...` and `X-Requested-With` headers
  (`amazon_prime_de.py:118`).
- **Schema-agnostic recursive walk** rather than a fixed payload model: `_collect_episodes` scans
  arbitrary nested mappings/lists, and the series title is the first of
  `seriesTitle`/`showTitle`/`parentTitle`, else `title`/`name`/`titleName` (`amazon_prime_de.py:227`,
  `amazon_prime_de.py:158`).
- **Identifier validation** only rejects empty ids and ids containing `/` or `?`
  (`amazon_prime_de.py:107`).
- **Numbering** is read from `seasonNumber`/`seasonIndex`/`season` and
  `episodeNumber`/`episodeIndex`/`episodeSequence` (ints > 0); an initial season hint comes from the
  first positive `seasonNumber` found anywhere. Episodes missing a number or id are dropped, and
  duplicates keep the first occurrence (`amazon_prime_de.py:241`, `amazon_prime_de.py:169`,
  `amazon_prime_de.py:267`).
- **Season ids** are `"{title_id}:season:{n|specials}"`; `None` seasons become `Specials`, sorted
  last (`amazon_prime_de.py:216`).
- **Release mapping**: the first present of `releaseDateTime`, `releaseDate`,
  `availabilityStartDate`, `originalReleaseDate`, `startDate`. Date-only or naive values are
  stamped `Europe/Berlin` then converted to UTC (with a German month-name fallback); numeric epochs
  are read as milliseconds when > 1e10, else seconds (`amazon_prime_de.py:288`,
  `amazon_prime_de.py:303`).
- **Duration** comes from `duration` (integer seconds) with a `runtime`/`durationString` text
  fallback, normalized to whole minutes (`amazon_prime_de.py:262`).
- **Always one `streaming` release per episode**, `available_until` never set; an undated episode
  gets `first_seen_release()`; url is `https://www.primevideo.com/-/{marketplace}/detail/{id}`
  (`amazon_prime_de.py:189`, `amazon_prime_de.py:184`).
- Uses the shared `request_with_retries()` and applies only `filter_obsolete_seasons()`
  (`amazon_prime_de.py:116`, `amazon_prime_de.py:149`).
