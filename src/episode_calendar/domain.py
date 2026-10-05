import enum
from datetime import timedelta


class ReleaseType(enum.StrEnum):
    STREAMING = "streaming"
    TV_BROADCAST = "tv_broadcast"


TV_BROADCAST_MATCH_TOLERANCE = timedelta(minutes=15)
