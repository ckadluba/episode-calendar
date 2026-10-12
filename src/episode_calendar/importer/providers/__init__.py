"""Provider adapter contracts and normalized data structures."""

from episode_calendar.importer.providers.ardmediathek import (
    ARDMediathekHTTPError,
    ARDMediathekMalformedResponseError,
    ARDMediathekProvider,
    ARDMediathekProviderError,
)
from episode_calendar.importer.providers.base import (
    NormalizedEpisode,
    NormalizedEpisodeRelease,
    NormalizedSeason,
    NormalizedSeries,
    ProviderAdapter,
)
from episode_calendar.importer.providers.channel4 import (
    Channel4HTTPError,
    Channel4MalformedResponseError,
    Channel4Provider,
    Channel4ProviderError,
)
from episode_calendar.importer.providers.joyn import (
    JoynGraphQLError,
    JoynHTTPError,
    JoynMalformedResponseError,
    JoynProvider,
    JoynProviderError,
)

__all__ = [
    "NormalizedEpisode",
    "NormalizedEpisodeRelease",
    "NormalizedSeason",
    "NormalizedSeries",
    "ProviderAdapter",
    "ARDMediathekHTTPError",
    "ARDMediathekMalformedResponseError",
    "ARDMediathekProvider",
    "ARDMediathekProviderError",
    "JoynGraphQLError",
    "JoynHTTPError",
    "JoynMalformedResponseError",
    "JoynProvider",
    "JoynProviderError",
    "Channel4HTTPError",
    "Channel4MalformedResponseError",
    "Channel4Provider",
    "Channel4ProviderError",
]
