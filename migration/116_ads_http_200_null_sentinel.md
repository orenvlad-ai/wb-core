# Ads `fullstats` HTTP 200 JSON-null sentinel

The production `ads_historical_recovery_v3` shape-evidence dry-run proved that
an exact singleton `/adv/v3/fullstats` request can return the JSON scalar
`null` on a successful response. The application received this as Python
`None`, rejected it as incomplete, wrote no snapshots and kept all requested
dates blocked.

`ads_historical_recovery_v4` recognizes this source signal only when all of
the following are exact:

- the request is made through the official `fullstats` source method;
- the HTTP status is `200`;
- the media type is `application/json` (an optional charset is allowed);
- the complete trimmed response body is the four bytes `null`.

The rule is not applied to the campaign manifest or any other JSON source.
Wrong status, content type, body or a generic fake-source `None` remains a
blocker. The plan request manifest records
`confirmation_signal=http_200_application_json_null` for this exact sentinel.
A batch omission still requires a separate singleton confirmation for every
omitted eligible campaign. Only after every campaign is reconciled may a global
day be persisted as `kind=empty`; no SKU-level synthetic zero is created.
