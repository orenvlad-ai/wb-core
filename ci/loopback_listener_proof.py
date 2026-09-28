"""Small read-only proof that a service PID owns its loopback listener."""

import inspect


def owned_loopback_listener(pid, port, *, proc_root="/proc"):
    import os
    from pathlib import Path

    root = Path(proc_root)
    owned_sockets = set()
    for fd in (root / str(pid) / "fd").iterdir():
        try:
            link = os.readlink(fd)
        except FileNotFoundError:
            continue
        if link.startswith("socket:[") and link.endswith("]"):
            owned_sockets.add(link[8:-1])
    with (root / "net" / "tcp").open(encoding="ascii") as table:
        next(table)
        return any(
            (parts := line.split())[1] == f"0100007F:{port:04X}"
            and parts[3] == "0A" and parts[9] in owned_sockets
            for line in table
        )


def remote_source() -> str:
    """Embed the exact tested function in SSH readback scripts."""
    return inspect.getsource(owned_loopback_listener)
