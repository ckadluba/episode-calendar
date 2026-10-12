"""Command-line entry point for configured provider imports."""

from __future__ import annotations

import argparse
import asyncio
import logging

from episode_calendar.importer.providers.amazon_prime_de import AmazonPrimeDEProvider
from episode_calendar.importer.providers.amazon_prime_uk import AmazonPrimeUKProvider
from episode_calendar.importer.providers.ardmediathek import ARDMediathekProvider
from episode_calendar.importer.providers.base import ProviderAdapter
from episode_calendar.importer.providers.bbc_iplayer import BBCIPlayerProvider
from episode_calendar.importer.providers.channel4 import Channel4Provider
from episode_calendar.importer.providers.joyn import JoynProvider
from episode_calendar.importer.providers.rtlplus import RTLPlusProvider
from episode_calendar.importer.providers.stv import STVProvider
from episode_calendar.importer.service import configured_platform, import_configured

logger = logging.getLogger(__name__)

PROVIDERS: tuple[tuple[str, type[ProviderAdapter]], ...] = (
    ("joyn", JoynProvider),
    ("rtlplus", RTLPlusProvider),
    ("bbc_iplayer", BBCIPlayerProvider),
    ("channel4", Channel4Provider),
    ("ardmediathek", ARDMediathekProvider),
    ("stv", STVProvider),
    ("amazon_prime_de", AmazonPrimeDEProvider),
    ("amazon_prime_uk", AmazonPrimeUKProvider),
)
PROVIDERS_BY_SLUG = dict(PROVIDERS)


async def _run_all() -> None:
    results = await asyncio.gather(
        *(import_configured(provider_slug, factory) for provider_slug, factory in PROVIDERS),
        return_exceptions=True,
    )
    failures = [result for result in results if isinstance(result, Exception)]
    for (provider_slug, _), result in zip(PROVIDERS, results, strict=True):
        if isinstance(result, Exception):
            provider_name = provider_slug
            try:
                provider_name = configured_platform(provider_slug).name
            except RuntimeError:
                pass
            logger.error("Provider import failed for %s: %s", provider_name, result)
    if failures:
        raise RuntimeError("one or more provider imports failed") from failures[0]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        prog="python -m episode_calendar.importer",
        description="Import configured provider catalogs",
    )
    parser.add_argument("provider", choices=(*PROVIDERS_BY_SLUG, "all"))
    args = parser.parse_args()
    if args.provider == "all":
        asyncio.run(_run_all())
    else:
        asyncio.run(import_configured(args.provider, PROVIDERS_BY_SLUG[args.provider]))


if __name__ == "__main__":
    main()
