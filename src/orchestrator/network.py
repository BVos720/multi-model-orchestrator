from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import httpx

# Tailscale's CGNAT range - every device on a tailnet gets a stable IP in
# here, reachable only over its WireGuard-encrypted, device-authenticated
# mesh. An address in this range (or a MagicDNS `*.ts.net` name) is already
# protected; anything else hitting Ollama's auth-less HTTP API is not.
TAILSCALE_CIDR = ipaddress.ip_network("100.64.0.0/10")


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


def is_tailscale_address(host: str) -> bool:
    """True if `host` (an IP or hostname) is already protected by Tailscale -
    its CGNAT IP range, or a MagicDNS `<machine>.<tailnet>.ts.net` name."""
    if host.endswith(".ts.net"):
        return True
    try:
        return ipaddress.ip_address(host) in TAILSCALE_CIDR
    except ValueError:
        return False


def connection_warning(url: str) -> str | None:
    """Flag a host URL that isn't loopback or Tailscale - Ollama has no
    built-in auth or TLS, so anything else is plain, unauthenticated HTTP.
    Returns None for a URL that's already protected (nothing to warn
    about), otherwise a one-line warning to show the user."""
    host = urlparse(url).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1") or is_tailscale_address(host):
        return None
    return (
        f"{host} isn't a Tailscale address - this connection is plain, unencrypted HTTP "
        "(Ollama has no built-in auth/TLS). Fine on a trusted LAN; never expose it beyond "
        "that. For real encryption, register this host by its Tailscale IP/MagicDNS name "
        'instead (see README "Networking two PCs").'
    )


async def ollama_reachable(url: str = "http://localhost:11434") -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{url}/api/tags")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def list_models(url: str, timeout: float = 5.0) -> list[str]:
    """Models already pulled on the Ollama instance at `url` - what a model
    picker should show as "ready now", vs. anything else the user types
    that `pull_model` would need to fetch first."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{url}/api/tags")
            if resp.status_code != 200:
                return []
            data = resp.json()
            return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except (httpx.HTTPError, ValueError):
        return []


async def pull_model(url: str, model: str, on_progress=None) -> bool:
    """Pull `model` onto the Ollama instance at `url`, via its own
    /api/pull endpoint - exactly what `ollama pull` does locally, but it
    works against ANY reachable Ollama instance over plain HTTP, remote
    workers included, with no SSH/CLI access to that machine needed.

    on_progress, if given, is called with each raw status dict Ollama
    streams back (a "status" string, plus "completed"/"total" byte counts
    once the download itself is underway) - use it to render progress.
    No timeout here: a multi-GB model can take a long time, same as the
    real `ollama pull` CLI. Returns True once Ollama reports "success",
    False on any error (bad model name, connection drop, disk full...)."""
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", f"{url}/api/pull", json={"name": model}) as resp:
                if resp.status_code != 200:
                    return False
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if on_progress:
                        on_progress(chunk)
                    if "error" in chunk:
                        return False
                    if chunk.get("status") == "success":
                        return True
    except httpx.HTTPError:
        return False
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


async def scan_tailscale_peers(port: int = 11434, timeout: float = 0.5) -> list[dict]:
    """Ask the local Tailscale daemon for its own peer list instead of
    brute-forcing a subnet - `tailscale status --json` already knows every
    device on the tailnet and its stable 100.x IP, regardless of what LAN
    (if any) that device is actually sitting on. Each peer is then probed
    the same way as `scan_for_workers` (same dict shape back). Returns []
    if the `tailscale` CLI isn't installed or isn't logged in/running -
    this is an alternative discovery source, not a requirement."""
    cli = shutil.which("tailscale")
    if not cli:
        return []
    try:
        proc = await asyncio.create_subprocess_exec(
            cli, "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        data = json.loads(stdout)
    except (OSError, json.JSONDecodeError):
        return []

    peers = data.get("Peer") or {}
    ips: list[str] = []
    for peer in peers.values():
        for addr in peer.get("TailscaleIPs") or []:
            try:
                if ipaddress.ip_address(addr).version == 4:
                    ips.append(addr)
            except ValueError:
                continue

    sem = asyncio.Semaphore(32)

    async def _probe(ip: str) -> dict | None:
        async with sem:
            return await probe_ollama(ip, port=port, timeout=timeout)

    results = await asyncio.gather(*[_probe(ip) for ip in ips])
    return [r for r in results if r]


def ollama_log_path() -> Path:
    """Where Ollama's own log lives on Windows. It already carries both
    what you'd want a "networking log" for (a `[GIN] ... 200 ... 127.0.0.1
    | GET "/api/tags"` line per incoming HTTP request - method, status,
    source IP, timing) and general server/model activity, so tailing this
    one file covers both without needing to parse anything ourselves."""
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Ollama" / "server.log"


def open_ollama_log_window() -> bool:
    """Pop open a separate, live-updating console window tailing Ollama's
    log - so right after registering this machine as a worker, you can
    watch requests actually arrive from the supervisor (or not) in real
    time, instead of guessing whether a connection got through.

    Windows-only (this project's whole worker-networking story already
    is); returns False there or if nothing's been logged yet (Ollama
    hasn't run), rather than opening an empty/erroring window."""
    if os.name != "nt":
        return False
    log_path = ollama_log_path()
    if not log_path.exists():
        return False
    title = "Ollama logs (live) - close this window to stop watching"
    command = (
        f"$Host.UI.RawUI.WindowTitle = '{title}'; "
        f"Get-Content -Path '{log_path}' -Wait -Tail 40"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoExit", "-Command", command],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return True
    except OSError:
        return False
