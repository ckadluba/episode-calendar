# Amazon Prime DE

The provider reads the JSON detail data used by the public German Prime Video web
application:

`GET https://www.primevideo.com/-/de/gp/video/api/getDetailPage`

The configured series identifier is the Prime Video title ID (`ASIN` or
`amzn1.dv.gti...`). The endpoint is not a documented consumer API and may change
without notice. Amazon's documented Prime Video APIs are partner APIs and require
appropriate partner access.

The response must contain episode objects with a title ID, season and episode
numbers, and a release or availability date. Date-only values are interpreted as
midnight in the German timezone; timestamps with an offset are converted to UTC.
