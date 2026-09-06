"""Provider adapter contracts and normalized data structures."""

from episode_calendar.providers.base import (
    NormalizedEpisode,
    NormalizedEpisodeRelease,
    NormalizedSeason,
    NormalizedSeries,
    ProviderAdapter,
)
from episode_calendar.providers.joyn import (
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
    "JoynGraphQLError",
    "JoynHTTPError",
    "JoynMalformedResponseError",
    "JoynProvider",
    "JoynProviderError",
]
