# Joyn Austria provider

`JoynProvider` retrieves public catalog metadata from Joyn Austria and returns the generic
normalized provider DTOs. It does not play or download video and does not require a Joyn user
account.

## Current API contract

The following contract was verified against the live Austria website and API on 2026-09-06,
using the public series page at [joyn.at](https://www.joyn.at/serien/so-denkt-oesterreich):

- GraphQL endpoint: `https://api.joyn.de/graphql`.
- The web application sends a GraphQL document by `POST` with `operationName`, `variables`, and
  `query`. The provider uses this full-document form.
- Requests include `Joyn-Platform: web`, `Joyn-Distribution-Tenant: JOYN_AT`, `Joyn-Country: AT`,
  a client-version header, and `x-api-key`.
- The public web API key is client configuration (`API_GW_API_KEY` in the web bundle), not a user
  credential. Set it at runtime as `JOYN_API_KEY`; never commit it.
- The series query uses `page(path: ...)` and returns a `SeriesPage` containing `series.id`,
  `title`, `description`, and `seasons`.
- Season results contain `id`, `number`, `numberOfEpisodes`, and `episodes`. Episode fields used
here are `id`, `number`, `title`, `airdate`, `endsAt`, and `path`.
- The web client requests episodes in pages of 20 with an offset. `JoynProvider` follows every
  page until `numberOfEpisodes` is reached and fails if a page is missing or inconsistent.
- Current responses expose `airdate` as a Unix timestamp; it is converted to a timezone-aware UTC
  datetime (preserving the instant). ISO-8601 values with an offset are also accepted.

The website currently uses persisted GET operations named `SeriesDetailNewPageStatic` and
`Season`. Their hashes and the web bundle's client version are deployment details that can change.
The provider sends the equivalent full POST documents because the web client itself falls back to
that form when a persisted query is unavailable.

## Assumptions and limitations

Mapping `airdate` to a `streaming` release is a domain interpretation; Joyn labels the field
`airdate` rather than documenting it as a release contract. `endsAt`, when present, is mapped to
`available_until`; missing `airdate` produces no release.
Episode URLs are derived from the returned relative `path`, and no separate release identifier is
currently exposed, so release `external_id` remains unset. These behaviors are isolated here and
may need adjustment if Joyn changes its schema.

Joyn's catalog API is private/undocumented. GraphQL fields, headers, pagination limits, the public
key, and operation behavior may change without notice. Authentication, playback, DRM, and import
scheduling are intentionally outside this provider.

Example:

```python
from episode_calendar.providers.joyn import JoynProvider

series = await JoynProvider(api_key="<JOYN_API_KEY>").get_series("so-denkt-oesterreich")
```
