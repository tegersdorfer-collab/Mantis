"""Begrenzt gleichzeitige Fast-Cloud-Anfragen und lokale Embedding-Aufrufe."""
import asyncio

GATE = asyncio.Lock()
