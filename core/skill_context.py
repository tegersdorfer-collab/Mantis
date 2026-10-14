"""
Geteilter Skill-Context (CTX) + bind().

Liegt bewusst in einem eigenen Modul ohne Abhängigkeit auf die Tool-Module,
damit alle core/skills/*.py ihn per `from core.skill_context import CTX`
importieren können, ohne Zirkel.

bind() MUTIERT das CTX-Singleton (kein Rebind). Dadurch sehen alle Module,
die CTX importiert haben, die gebundenen Werte — eine Rebind-Zuweisung
(`CTX = ctx`) würde importierte Referenzen dagegen stale lassen.
"""
import logging
from contextvars import ContextVar
from dataclasses import dataclass, fields

log = logging.getLogger("core.skills")


@dataclass
class SkillContext:
    lzg: object = None          # LZG-Instanz (Memory)
    llm: object = None          # lokaler Embedding-Provider
    search: object = None       # WebSearch
    reminders: object = None    # ReminderStore
    dashboard: object = None    # DashboardReader
    channel: object = None      # CommunicationChannel (Telegram) – für proaktive Hinweise


CTX = SkillContext()


@dataclass(frozen=True)
class InboxTurnContext:
    item_id: int | None = None
    reply_reference: str | None = None


INBOX_TURN_CONTEXT: ContextVar[InboxTurnContext | None] = ContextVar(
    "inbox_turn_context", default=None,
)


def bind(ctx: SkillContext) -> None:
    from core import tools as T
    for f in fields(SkillContext):
        setattr(CTX, f.name, getattr(ctx, f.name))
    log.info(f"Skills gebunden – {len(T.REGISTRY)} Tools verfügbar")
