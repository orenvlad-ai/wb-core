"""Private journal transport. Its fixed command line keeps operator argv out of journald."""
import json
import socket


def forward(incoming, sink="/run/systemd/journal/socket"):
    dropped = 0
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as out:
        out.setblocking(False)
        while payload := incoming.recv(8192):
            try:
                if dropped:
                    record = json.loads(payload)
                    record["dropped_before"] = record.get("dropped_before", 0) + dropped
                    payload = json.dumps(record, separators=(",", ":")).encode("ascii")
                out.sendto(b"SYSLOG_IDENTIFIER=wbc-procedure\nPRIORITY=6\nMESSAGE=" + payload + b"\n", sink)
            except (OSError, ValueError):
                dropped = min(dropped + 1, 2**31 - 1)
            else:
                dropped = 0


if __name__ == "__main__":
    with socket.socket(fileno=0) as channel:
        forward(channel)

