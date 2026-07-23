# Ads no-statistics envelope recovery

The first production dry-run of `ads_historical_recovery_v2` proved that the
campaign-time filtering and singleton fallback were active, but the official
singleton `fullstats` no-statistics result arrived as a JSON object on a
successful transport response instead of a response list. The plan remained
fail-closed with 61 unconfirmed dates and an empty write set.

`ads_historical_recovery_v3` recognizes the same exact official structured
signal independent of the transport status: `status=400`, origin
`camp-api-public-cache` and detail
`there are no statistics for this advertising period`. No other mapping,
empty list, changed detail/origin/status, malformed JSON or transport error is
accepted. The application layer still requires singleton confirmation for an
ID omitted by a batch response and persists zero only as a globally confirmed
`kind=empty` snapshot.

Dry-run remains read-only. Apply, backup, exact fingerprint, approval,
target/non-target drift, transaction and readback gates are unchanged.
