"""Mantis Forge — semi-autonomes Entwicklungssystem.

Eigener Prozess, eigener launchd-Job. Importiert aus Mantis ausschließlich
`core.db`; mit dem laufenden Assistant wird nur über HTTP geredet. Diese Grenze
sorgt dafür, dass ein Absturz der Forge den Concierge nicht mitnimmt.
"""
from pathlib import Path

# Das Haupt-Checkout, auf dem Mantis produktiv läuft. Hier wird NIE gearbeitet.
MANTIS_REPO = Path(__file__).resolve().parent.parent

# Wurzel für die isolierten Worktrees — bewusst außerhalb des Repos, damit
# Suchen, Linter und die Test-Suite sie nicht mit einsammeln.
FORGE_ROOT = Path.home() / "Mantis-forge"
