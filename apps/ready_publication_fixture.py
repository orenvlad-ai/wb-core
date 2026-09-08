"""Immediate fixture seeding through the mandatory production CAS API.

Concurrency tests capture ExpectedReady before their barrier and call the real
writer themselves; this adapter only seeds prebuilt existing smoke fixtures.
"""


def save_ready_fixture(runtime, **kwargs):
    if "expected" not in kwargs:
        kwargs["expected"] = runtime.prepare_sheet_vitrina_ready_publication(
            bundle_version=kwargs["current_state"].bundle_version,
            as_of_date=kwargs["plan"].as_of_date)
    return runtime.save_sheet_vitrina_ready_snapshot(**kwargs)
