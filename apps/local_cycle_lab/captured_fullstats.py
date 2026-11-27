"""Prepare a private anonymized WB fullstats envelope and verify it offline."""

import argparse
from collections import defaultdict
from datetime import date
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from .http import Response
from .state import atomic_json, read_json

METRICS = ("views", "clicks", "atbs", "orders", "sum", "sum_price")


def prepare(capture, destination):
    if destination.exists():
        raise FileExistsError(destination)
    with capture.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        source.seek(0)
        envelope = json.loads(source.readline())
    if envelope["http"] != 200 or not envelope["data"]:
        raise ValueError("Expected an archived successful fullstats response")
    products = {}

    def product(value):
        return products.setdefault(int(value), len(products) + 101)

    metrics = set(METRICS) | {"canceled", "shks", "cpc", "cr", "ctr"}

    def known(row, fields):
        if not isinstance(row, dict) or row.keys() - fields:
            raise ValueError("Unreviewed fields in private response")

    ids = [campaign["advertId"] for campaign in envelope["data"]]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate campaign in archived response")
    for idx, campaign in enumerate(envelope["data"], 601):
        known(campaign, metrics | {"advertId", "currency", "boosterStats", "days"})
        campaign["advertId"] = idx
        for row in campaign.get("boosterStats", []):
            known(row, {"nm", "date", "avg_position"})
            row["nm"] = product(row["nm"])
        for day in campaign["days"]:
            known(day, metrics | {"date", "apps"})
            for app in day["apps"]:
                known(app, metrics | {"appType", "nms"})
                for row in app["nms"]:
                    known(row, metrics | {"nmId", "name"})
                    row["nmId"] = product(row["nmId"])
                    if "name" in row:
                        row["name"] = "Lab " + str(row["nmId"])
    # Keep actual numeric relationships and missing values private. No response is shipped in the repository.
    with TemporaryDirectory(dir=destination.parent) as folder:
        temporary = Path(folder) / "response.json"
        atomic_json(
            temporary,
            dict(source_sha256=digest, start=envelope["start"], end=envelope["end"], data=envelope["data"]),
        )
        os.link(temporary, destination)


def verify(fixture):
    from packages.adapters.wb_promotion import HttpBackedWbPromotionSource
    from packages.application.ads_historical_recovery import _aggregate_day_rows, _fullstats_rows
    from .full_history import isolation_evidence

    isolation_evidence()
    envelope = read_json(fixture)
    data, start, end = envelope["data"], envelope["start"], envelope["end"]
    ids = [a["advertId"] for a in data]
    expected = defaultdict(lambda: [Decimal(0)] * len(METRICS))
    for campaign in data:
        for day in campaign["days"]:
            for app in day["apps"]:
                for row in app["nms"]:
                    acc = expected[day["date"][:10], row["nmId"]]
                    for i, key in enumerate(METRICS):
                        acc[i] += Decimal(str(row[key]))
    calls = []

    def offline(request, *, timeout):
        url = urlsplit(request.full_url)
        if (
            request.get_method() != "GET"
            or request.data is not None
            or (url.scheme, url.netloc, url.path)
            != ("https", "advert-api.wildberries.ru", "/adv/v3/fullstats")
            or parse_qs(url.query) != dict(ids=[",".join(map(str, ids))], beginDate=[start], endDate=[end])
        ):
            raise ValueError("Unexpected archived fullstats request")
        calls.append(url.path)
        return Response(data)

    os.environ["WB_API_TOKEN"] = "lab-only"
    with patch("urllib.request.urlopen", offline):
        payload = HttpBackedWbPromotionSource().fetch_fullstats(ids, begin_date=start, end_date=end)
    rows, seen = _fullstats_rows(
        payload,
        start=date.fromisoformat(start),
        end=date.fromisoformat(end),
        allowed_campaign_ids=set(ids),
        require_all_campaigns=True,
    )
    actual = {
        (day, item["nm_id"]): [Decimal(str(item["ads_" + k])) for k in METRICS]
        for day, items in rows.items()
        for item in _aggregate_day_rows(items, day)
    }
    if len(calls) != 1 or seen != set(ids) or actual != expected or not actual:
        raise ValueError("Archived fullstats totals differ from source rows")
    return dict(
        source_sha256=envelope["source_sha256"],
        fixture_sha256=hashlib.sha256(fixture.read_bytes()).hexdigest(),
        campaigns=len(ids),
        days=len(rows),
        input_product_rows=sum(map(len, rows.values())),
        sku_days=len(actual),
        metric_values=len(actual) * len(METRICS),
        requests=calls,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("private_fixture", type=Path)
    args = parser.parse_args()
    prepare(args.capture, args.private_fixture)
