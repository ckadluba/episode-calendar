# Amazon Prime UK (`amazon_prime_uk`)

`AmazonPrimeUKProvider` reuses the same detail-page adapter as `amazon_prime_de`
(`AmazonPrimeProvider`) and only changes the marketplace. The configured `id` is the same Prime
Video title ID (an ASIN or `amzn1.dv.gti...`).

## Specialties and deviations

- **Only class attributes are overridden**: `provider_slug="amazon_prime_uk"`,
  `provider_label="Amazon Prime UK"`, the base-url/timeout settings, and `marketplace="gb"`
  (`amazon_prime_uk.py:9`). The endpoint becomes
  `https://www.primevideo.com/-/gb/gp/video/api/getDetailPage`.
- **Reuses the DE mapping unchanged**, so it shares every DE specialty documented in
  [Amazon Prime DE](amazon_prime_de.md).
- **Inherited quirks**: the `Accept-Language: de-DE,...` header and the `Europe/Berlin` timezone
  used for date-only/naive values are **not** overridden, so UK responses are requested with a
  German locale and date-only UK strings are interpreted in Berlin time (`amazon_prime_de.py:127`,
  `amazon_prime_de.py:303`).
