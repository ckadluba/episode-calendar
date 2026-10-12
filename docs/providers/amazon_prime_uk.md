# Amazon Prime UK

The `amazon_prime_uk` provider uses the same Prime Video detail-page adapter as
`amazon_prime_de`. It calls the public, undocumented detail-page API with the UK marketplace URL
and normalizes seasons, episodes, and release dates into the shared data model.

The configured series identifier is the Prime Video title ID. For the first test,
`0JAJ59KRPSJY5QIDTK0WTBWMR6` is configured for `LOL: Last One Laughing UK`.

The API is not documented as a public consumer API and may change at any time.
