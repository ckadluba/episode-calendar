"""Amazon Prime Video United Kingdom detail-page adapter."""

from episode_calendar.providers.amazon_prime_de import AmazonPrimeProvider


class AmazonPrimeUKProvider(AmazonPrimeProvider):
    """Amazon Prime Video United Kingdom adapter."""

    provider_slug = "amazon_prime_uk"
    provider_label = "Amazon Prime UK"
    base_url_setting = "amazon_prime_uk_base_url"
    timeout_setting = "amazon_prime_uk_timeout_seconds"
    marketplace = "gb"
