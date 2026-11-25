"""Read-only response tape for the real WBC HTTP clients, in process."""
from __future__ import annotations

from copy import deepcopy
from email.message import Message
from io import BytesIO
import json
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlsplit

from .sources import render


class Response(BytesIO):
    status = 200

    def __init__(self, payload):
        super().__init__(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"


def zero_ad_metrics(node):
    if isinstance(node, list):
        for item in node:
            zero_ad_metrics(item)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in {"views", "clicks", "atbs", "orders", "sum", "sum_price"}:
                node[key] = 0
            else:
                zero_ad_metrics(value)


class HttpTape:
    def __init__(self, replay):
        self.replay = replay
        self.injected_errors = []

    def open(self, request, *, timeout=None):
        url = urlsplit(request.full_url)
        query = sorted(parse_qsl(url.query, keep_blank_values=True))
        body = json.loads(request.data) if request.data else None
        for entry in self.replay.scenario["http"]:
            expected_request = render(entry["request"], self.replay.current_day)
            if (url.scheme != "https" or url.netloc != entry["host"] or url.path != entry["path"]
                or request.get_method() != entry["method"] or query != sorted(entry["query"].items())
                or body != expected_request):
                continue
            key = entry["source"]
            payload = deepcopy(entry.get("responses_by_day", {}).get(self.replay.current_day, entry["response"]))
            if key == "ads_compact" and entry["query"]["beginDate"] == self.replay.scenario["days"][0]:
                if self.replay.variant == "missing":
                    self.replay.observe(key, {}, 0, pages=1, boundary="http_response")
                    self.injected_errors.append((key, entry["query"]["beginDate"], 503))
                    raise HTTPError(request.full_url, 503, "offline fixture", {}, BytesIO(b"{}"))
                if self.replay.variant == "zero":
                    zero_ad_metrics(payload)
            rows = payload
            if entry["rows_path"]:
                for part in entry["rows_path"].split("."):
                    rows = rows[part]
            self.replay.observe(key, payload, len(rows), pages=1, boundary="http_response",
                                request_bytes=len(request.data or b""))
            return Response(payload)
        self.replay.reject(f"{request.get_method()} {url.netloc}{url.path} query={query} body={body}")
