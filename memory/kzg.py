"""
Kurzzeitgedächtnis (KZG) – aktiver Gesprächskontext.
In-Memory, geht beim Neustart verloren (gewollt).
"""
from dataclasses import dataclass, field
from datetime import datetime
from llm.base import Message
import config


@dataclass
class Turn:
    role: str
    content: str
    timestamp: datetime = field(default_factory=datetime.now)


class KZG:
    """
    Kurzzeitgedächtnis: Speichert den laufenden Gesprächsverlauf.
    Wenn das Limit erreicht wird, werden ältere Turns in einem Checkpoint-Summary
    komprimiert (MemGPT-Muster) statt hart abgeschnitten.
    """

    def __init__(self, max_turns: int | None = None):
        self.max_turns = max_turns or config.KZG_MAX_TURNS
        self._turns: list[Turn] = []
        self._session_start = datetime.now()
        self._checkpoint_summary: str | None = None  # komprimierte ältere Turns
        self._needs_checkpoint: bool = False          # Orchestrator soll Summary bauen

    def add(self, role: str, content: str) -> None:
        if not isinstance(content, str) or not content.strip():
            return
        self._turns.append(Turn(role=role, content=content))
        if len(self._turns) > self.max_turns:
            # Wenn noch kein Checkpoint läuft: markieren statt hart schneiden
            if not self._needs_checkpoint:
                self._needs_checkpoint = True
            # Hard-Fallback: nie mehr als max_turns * 1.5 im RAM
            if len(self._turns) > int(self.max_turns * 1.5):
                self._turns = self._turns[-self.max_turns:]

    def should_checkpoint(self) -> bool:
        """True wenn der Orchestrator einen Checkpoint-Summary generieren soll."""
        return self._needs_checkpoint

    def apply_checkpoint(self, summary: str, compress_count: int) -> None:
        """
        Speichert den generierten Summary und entfernt die zusammengefassten Turns.
        compress_count: wie viele der ältesten Turns komprimiert wurden.
        """
        self._checkpoint_summary = summary
        self._turns = self._turns[compress_count:]
        self._needs_checkpoint = False

    def get_messages(self) -> list[Message]:
        return [Message(role=t.role, content=t.content) for t in self._turns]

    def recent_messages(self, max_tokens: int = 3000, min_turns: int = 4) -> list[Message]:
        """
        Jüngste Turns innerhalb eines Token-Budgets (4 Zeichen ≈ 1 Token).
        Wenn ein Checkpoint-Summary existiert, wird er als erster user-Turn eingebettet
        damit der Kontext früherer Turns nicht verloren geht.
        """
        selected: list[Turn] = []
        budget = max_tokens
        for t in reversed(self._turns):
            cost = len(t.content) // 4 + 1
            if selected and budget - cost < 0 and len(selected) >= min_turns:
                break
            selected.append(t)
            budget -= cost
        selected.reverse()
        messages = [Message(role=t.role, content=t.content) for t in selected]
        if self._checkpoint_summary and messages:
            summary_msg = Message(
                role="user",
                content=f"[Gesprächs-Zusammenfassung früherer Turns]\n{self._checkpoint_summary}",
            )
            messages = [summary_msg] + messages
        return messages

    def get_turns_for_summary(self) -> list[Turn]:
        """Gibt die älteste Hälfte der Turns zurück – für Checkpoint-Generierung."""
        half = max(4, len(self._turns) // 2)
        return self._turns[:half]

    def get_turns(self) -> list[Turn]:
        return list(self._turns)

    def get_recent_turns(self, n: int = 6) -> list[Turn]:
        """Gibt die n jüngsten Turns zurück — für Recall Gate."""
        return self._turns[-n:]

    def clear(self) -> None:
        self._turns = []
        self._session_start = datetime.now()

    def is_empty(self) -> bool:
        return len(self._turns) == 0

    def token_estimate(self) -> int:
        """Grobe Schätzung der Token-Anzahl (4 Zeichen ≈ 1 Token)."""
        total_chars = sum(len(t.content) for t in self._turns)
        return total_chars // 4

    def last_user_message(self) -> str | None:
        for turn in reversed(self._turns):
            if turn.role == "user":
                return turn.content
        return None

    def summary_for_compression(self) -> str:
        """Gibt KZG-Inhalt für LZG-Komprimierung zurück."""
        lines = []
        for t in self._turns:
            prefix = "Timo" if t.role == "user" else "Mantis"
            lines.append(f"{prefix}: {t.content}")
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self._turns)

    def __repr__(self) -> str:
        return f"KZG({len(self._turns)} turns, ~{self.token_estimate()} tokens)"
