"""Retryfähiger uvicorn-Server für die eigenständige Gym-App."""

from __future__ import annotations

import asyncio
import ipaddress
import logging

import uvicorn

from web.gym_app import create_gym_app


log = logging.getLogger(__name__)
_TAILSCALE_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)


def validate_host(host: str) -> str:
    """Akzeptiert nur eine konkrete Loopback- oder Tailscale-Adresse."""
    address = ipaddress.ip_address(host)
    if not address.is_loopback and not any(address in network for network in _TAILSCALE_NETWORKS):
        raise ValueError("GYM_HOST must be a loopback or explicit Tailscale IP")
    return str(address)


async def run_gym_server(
    host: str,
    port: int,
    *,
    attempts: int | None = None,
    base_delay: float = 5.0,
    max_delay: float = 300.0,
) -> None:
    """Startet den Gym-Server und wiederholt fehlgeschlagene Bind-Versuche."""
    try:
        validated_host = validate_host(host)
    except ValueError as error:
        log.error("Gym-App Host ungültig (%s): %s", host, error)
        return

    attempt = 0
    retry_delay = base_delay
    while attempts is None or attempt < attempts:
        attempt += 1
        server = None
        try:
            config = uvicorn.Config(
                create_gym_app(),
                host=validated_host,
                port=port,
                log_level="warning",
            )
            server = uvicorn.Server(config)
            await server.serve()
        except (SystemExit, OSError) as error:
            log.warning(
                "Gym-App Bind-Versuch %s fehlgeschlagen (%s: %s)",
                attempt,
                type(error).__name__,
                error,
            )
        except Exception as error:
            log.warning(
                "Gym-App Startversuch %s fehlgeschlagen (%s: %s)",
                attempt,
                type(error).__name__,
                error,
            )
        else:
            if server is not None and server.should_exit:
                return
            log.warning("Gym-App Server wurde ohne Stop-Anforderung beendet (Versuch %s)", attempt)

        if attempts is not None and attempt >= attempts:
            return
        retry_delay = min(retry_delay, max_delay)
        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, max_delay)
