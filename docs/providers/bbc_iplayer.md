# BBC iPlayer provider

`BBCIPlayerProvider` reads public programme metadata from the BBC iPlayer Business Layer API.
It accepts BBC programme PIDs, for example `m002csng` for *The Celebrity Traitors* and
`b006m8dq` for *Strictly Come Dancing*.

The BBC entries in `config/series.json` are objects with `id` and an optional `comment` field.
The comment is documentation for humans; the importer uses only the PID in `id`. This is the
valid-JSON equivalent of an inline comment and avoids maintaining a separate mapping file.

The provider uses `/programmes/{pid}` for series metadata, the paginated
`/programmes/{pid}/episodes` endpoint for the catalogue, and the configured BBC channel schedule
endpoints for planned future episodes. Planned broadcasts use their scheduled start as the
`streaming` release, even while the iPlayer episode is still unavailable. Already available
episodes use the first original version's iPlayer availability start and end. If no version
availability is present, the provider falls back to the episode's `release_date_time`.

Schedule channels and look-ahead days can be configured with `BBC_IPLAYER_SCHEDULE_CHANNELS` and
`BBC_IPLAYER_SCHEDULE_DAYS`; the defaults are BBC One London and 14 days.

BBC PIDs do not always expose a separate season entity. The adapter extracts `Series N` or
`Cyfres N` from the episode subtitle and puts unnumbered episodes into a `Specials` season.
Episodes without either an availability start or a release date are omitted because they cannot
be represented in the release calendar.

The API is public web metadata rather than a versioned partner contract. BBC may change its
fields or availability behaviour, and the resulting iPlayer links can require a UK account or
TV licence and may not play from Austria. No BBC credentials are required by the importer.
