from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpx


def local_ip() -> str:
    """Best-effort LAN-facing IP address of this machine. Opens a UDP
    "connection" (no packet actually sent - UDP has no handshake) to pick
    which local interface the OS would route through, which is the
    standard trick for this since there's no single authoritative
    "my IP" on a multi-NIC machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def ollama_reachable(url: str = "http://localhost:11434") -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{url}/api/tags")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def _tcp_open(ip: str, port: int, timeout: float) -> bool:
    """Raw-socket connect check, used as a fast pre-filter before the real
    HTTP probe.

    This deliberately avoids httpx/asyncio's higher-level timeout here:
    on Windows, cancelling a pending `loop.create_connection` doesn't
    reliably abort the underlying OS-level connect attempt, so a /24
    sweep of mostly-empty addresses can take the platform's TCP connect
    timeout (tens of seconds) *per host* instead of the timeout we asked
    for - turning a 254-address sweep into minutes. Explicitly closing a
    raw non-blocking socket on timeout does abort it immediately, so this
    stays fast everywhere. Only addresses that pass this check go on to
    the (slower, but now rare) HTTP probe below."""
    loop = asyncio.get_event_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        await asyncio.wait_for(loop.sock_connect(sock, (ip, port)), timeout=timeout)
        return True
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        sock.close()


async def probe_ollama(ip: str, port: int = 11434, timeout: float = 0.5) -> dict | None:
    """Check one address for a real Ollama server, not just an open port.

    A bare TCP connect can't tell "Ollama" apart from "something else
    happens to be listening here" - requiring a 200 with the JSON shape
    `/api/tags` actually returns is what makes this a worker-discovery
    probe rather than a generic port scan."""
    url = f"http://{ip}:{port}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{url}/api/tags")
            if resp.status_code != 200:
                return None
            data = resp.json()
            models = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
            return {"ip": ip, "url": url, "models": models}
    except (httpx.HTTPError, ValueError):
        return None


async def scan_for_workers(
    port: int = 11434,
    timeout: float = 0.3,
    concurrency: int = 128,
) -> list[dict]:
    """Sweep this machine's local /24 subnet for other PCs running Ollama.

    Two passes, both bounded by `concurrency` so a /24 sweep doesn't open
    254 sockets at once: a fast raw-TCP pre-filter over every address
    first (see `_tcp_open`), then the real HTTP `/api/tags` probe only
    against whichever few addresses actually have something listening -
    so a full sweep usually finishes in well under a couple of seconds on
    a LAN. Each match is a dict with "ip", "url", and whatever "models"
    that machine already has pulled - this machine's own IP is skipped
    since it's never its own worker. Returns [] if this machine has no
    usable LAN interface (e.g. genuinely offline)."""
    me = local_ip()
    if me == "127.0.0.1":
        return []
    net = ipaddress.ip_network(f"{me}/24", strict=False)
    sem = asyncio.Semaphore(concurrency)
    targets = [str(h) for h in net.hosts() if str(h) != me]

    async def _check_open(ip: str) -> str | None:
        async with sem:
            return ip if await _tcp_open(ip, port, timeout) else None

    open_ips = [ip for ip in await asyncio.gather(*[_check_open(ip) for ip in targets]) if ip]

    async def _check_ollama(ip: str) -> dict | None:
        async with sem:
            return await probe_ollama(ip, port=port, timeout=max(timeout, 0.5))

    results = await asyncio.gather(*[_check_ollama(ip) for ip in open_ips])
    return [r for r in results if r]
