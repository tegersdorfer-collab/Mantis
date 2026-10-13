"""Sicherer, modellfreier Fast-Path für Codex-Aufträge aus Discord-DMs."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from communication.base import IncomingMessage

log = logging.getLogger(__name__)

MAX_REMOTE_RUNS = 2
DEFAULT_TIMEOUT_SECONDS = 45 * 60
CONFIRMATION_TTL_SECONDS = 10 * 60
TERMINATION_GRACE_SECONDS = 3
_REMOTE_ID_RE = re.compile(r"^remote-[a-f0-9]{12}$")
_CODEX_ID_RE = re.compile(r"^codex-task\.[A-Za-z0-9_-]+$")
_ALIAS_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,31}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
_EFFORT_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_START_PATTERNS = (
    re.compile(r"^\s*codex\s*:\s*(.*?)\s*$", re.IGNORECASE | re.DOTALL),
    re.compile(r"^\s*lass\s+codex\b(.*?)\s+machen[.!?]?\s*$", re.IGNORECASE | re.DOTALL),
)
_WRITE_WORDS = re.compile(
    r"\b(schreib\w*|implement\w*|erstell\w*|änder\w*|aender\w*|fix\w*|"
    r"bau\w*|füge\w*|entfern\w*|create\w*|modify\w*|add\w*|remove\w*|"
    r"update\w*|refactor\w*|write\w*)\b",
    re.IGNORECASE,
)
_READ_WORDS = re.compile(
    r"\b(analysier\w*|prüf\w*|pruef\w*|review\w*|lese\w*|erklär\w*|"
    r"erklaer\w*|erklär\w*|explain\w*|inspect\w*|frage\w*|read\w*)\b",
    re.IGNORECASE,
)
_CONFIRMATIONS = {"ja", "start"}
_REJECTIONS = {"nein"}
_REGISTRY_FIELDS = {"pid", "start", "dir", "run", "title"}


@dataclass
class AgentRemoteConfig:
    """Vertrauenswürdige Laufzeitkonfiguration; Chattext kommt hier nie hinein."""

    enabled: bool = False
    allowed_user_ids: Sequence[str] = ()
    repos: Mapping[str, str | Path] = field(default_factory=lambda: {"mantis": "~/Mantis"})
    state_dir: str | Path | None = None
    home_dir: str | Path | None = None
    task_binary: str | Path = "~/.local/bin/codex-task"
    ps_binary: str | Path = "~/.local/bin/codex-ps"
    model: str = "gpt-6-luna"
    effort: str = "max"
    test_args: Mapping[str, Sequence[str]] = field(default_factory=dict)
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_running: int = MAX_REMOTE_RUNS

    @classmethod
    def from_settings(cls) -> AgentRemoteConfig:
        """Lädt nur vertrauenswürdige Settings; ungültiges JSON ergibt leere Allowlists."""
        import config

        repos = _parse_json_mapping(getattr(config, "AGENT_REMOTE_REPOS", ""))
        tests = _parse_json_mapping(getattr(config, "AGENT_REMOTE_TEST_ARGS", ""))
        valid_tests: dict[str, tuple[str, ...]] = {}
        for alias, vector in tests.items():
            if isinstance(vector, list) and vector and all(
                isinstance(part, str) and part for part in vector
            ):
                valid_tests[alias] = tuple(vector)
        return cls(
            enabled=bool(getattr(config, "AGENT_REMOTE_ENABLED", False)),
            allowed_user_ids=getattr(config, "DISCORD_ALLOWED_USER_IDS", ()),
            repos={key: value for key, value in repos.items() if isinstance(value, str)},
            state_dir=Path.home() / "Library" / "Caches" / "mantis" / "agent-remote",
            home_dir=Path.home(),
            task_binary=Path.home() / ".local" / "bin" / "codex-task",
            ps_binary=Path.home() / ".local" / "bin" / "codex-ps",
            model=getattr(config, "CODEX_TASK_MODEL", "") or "gpt-6-luna",
            effort=getattr(config, "CODEX_TASK_EFFORT", "") or "max",
            test_args=valid_tests,
        )


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    requester_id: str
    dm_id: str
    repo_alias: str
    repo_path: str
    task: str
    mode: str
    model: str
    effort: str
    created_at: float
    expires_at: float
    branch: str | None = None
    worktree: str | None = None
    signature: str = ""


@dataclass
class Clarification:
    requester_id: str
    dm_id: str
    body: str
    repo_alias: str | None
    task: str | None
    stage: str


@dataclass
class RunRecord:
    remote_id: str
    requester_id: str
    dm_id: str
    repo_alias: str
    repo_path: str
    mode: str
    model: str
    effort: str
    status: str
    created_at: float
    deadline: float
    tmp_dir: str
    cwd: str
    branch: str | None = None
    worktree: str | None = None
    pid: int | None = None
    pgid: int | None = None
    process_identity: str | None = None
    registry_id: str | None = None
    registry_run: str | None = None
    ended_at: float | None = None
    notification_sent_at: float | None = None
    test_status: str | None = None
    test_pid: int | None = None
    test_pgid: int | None = None
    test_identity: str | None = None
    test_deadline: float | None = None
    test_output: str | None = None
    test_exit_code: int | None = None


def _parse_json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}

    def unique_pairs(pairs):
        parsed = {}
        for key, item in pairs:
            if key in parsed:
                raise ValueError("duplicate key")
            parsed[key] = item
        return parsed

    try:
        parsed = json.loads(value, object_pairs_hook=unique_pairs)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _slug(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return value[:28].strip("-") or "aufgabe"


def _read_key_value_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in _REGISTRY_FIELDS:
                values[key] = value
    except OSError:
        return {}
    return values


def _read_text(path: Path, limit: int | None = None) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return text if limit is None else text[-limit:]


def _process_identity(pid: int) -> str | None:
    """Liefert eine hochauflösende Prozessstart-ID; eine wiederverwendete PID passt nicht."""
    if sys.platform == "darwin":
        class ProcBSDInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]

        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
            proc_pidinfo = libproc.proc_pidinfo
            proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.POINTER(ProcBSDInfo),
                ctypes.c_int,
            ]
            proc_pidinfo.restype = ctypes.c_int
            info = ProcBSDInfo()
            size = proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
            if size != ctypes.sizeof(info) or info.pbi_pid != pid:
                return None
            return f"darwin:{info.pbi_start_tvsec}.{info.pbi_start_tvusec:06d}"
        except (AttributeError, OSError, ValueError):
            return None
    if sys.platform.startswith("linux"):
        try:
            stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            fields = stat_text[stat_text.rfind(")") + 2:].split()
            return f"linux:{fields[19]}" if len(fields) > 19 else None
        except (OSError, IndexError):
            return None
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    identity = result.stdout.strip()
    return identity if result.returncode == 0 and identity else None


def _verified_signal_group(pid: int, pgid: int, kind: str, expected_identity: str) -> bool:
    """Signale nur die bekannte Session, nachdem PID, PGID und Startzeit gepasst haben."""
    try:
        if pid <= 0 or pgid != pid or os.getpgid(pid) != pgid:
            return False
    except (ProcessLookupError, PermissionError, OSError):
        return False
    if _process_identity(pid) != expected_identity:
        return False
    sig = signal.SIGTERM if kind == "TERM" else signal.SIGKILL
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


class AgentRemoteService:
    """DM-Dispatcher, Statusspeicher und Prozessaufsicht für codex-task."""

    def __init__(
        self,
        config: AgentRemoteConfig,
        *,
        discord_channel=None,
        persist: Callable[..., None] | None = None,
        popen: Callable[..., subprocess.Popen] | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess] | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Any] | None = None,
        process_identity: Callable[[int], str | None] | None = None,
        process_group: Callable[[int], int] | None = None,
        signal_process_group: Callable[[int, int, str, str], bool] | None = None,
    ):
        self.config = config
        self.discord_channel = discord_channel
        self.persist = persist
        self._popen = popen or subprocess.Popen
        self._command_runner = command_runner or subprocess.run
        self._clock = clock or time.time
        self._sleep = sleep or asyncio.sleep
        self._process_identity = process_identity or _process_identity
        self._process_group = process_group or os.getpgid
        self._signal_process_group = signal_process_group or _verified_signal_group
        home = Path(config.home_dir or Path.home()).expanduser().resolve()
        self.home_dir = home
        state = config.state_dir or home / "Library" / "Caches" / "mantis" / "agent-remote"
        self.state_dir = Path(state).expanduser().resolve()
        self.runs_dir = self.state_dir / "runs"
        self.pending: dict[str, Proposal] = {}
        self.clarifications: dict[str, Clarification] = {}
        self._consumed_confirmations: dict[tuple[str, str, str], float] = {}
        self.runs: dict[str, RunRecord] = {}
        self._routes: dict[str, Any] = {}
        self._tasks: set[asyncio.Task] = set()
        self._notification_retrying: set[str] = set()
        self._lock = asyncio.Lock()
        if config.enabled:
            self._load_runs()

    @classmethod
    def from_settings(cls, *, discord_channel=None, persist=None) -> AgentRemoteService:
        return cls(
            AgentRemoteConfig.from_settings(),
            discord_channel=discord_channel,
            persist=persist,
        )

    @property
    def active_count(self) -> int:
        return sum(
            record.status in {"preparing", "running", "cancelling"}
            or record.test_status in {"preparing", "running", "cancelling"}
            for record in self.runs.values()
        )

    def _is_trusted_dm(self, msg: IncomingMessage) -> bool:
        raw = msg.raw
        if raw is None or getattr(raw, "guild", None) is not None:
            return False
        author = getattr(raw, "author", None)
        if author is None or getattr(author, "bot", False):
            return False
        raw_user = str(getattr(author, "id", ""))
        if raw_user != str(msg.sender_id) or raw_user not in {
            str(value).strip() for value in self.config.allowed_user_ids if str(value).strip()
        }:
            return False
        return True

    @staticmethod
    def _start_body(text: str) -> str | None:
        for pattern in _START_PATTERNS:
            match = pattern.match(text)
            if match:
                return match.group(1).strip()
        return None

    @staticmethod
    def _command_kind(text: str) -> tuple[str, str | None] | None:
        normalized = text.strip()
        folded = normalized.casefold()
        if folded == "was läuft?":
            return "list", None
        if folded == "ergebnis":
            return "latest", None
        for kind, pattern in (
            ("cancel", r"^abbrechen\s+(\S+)$"),
            ("diff", r"^diff\s+(\S+)$"),
            ("tests", r"^tests\s+in\s+(\S+)$"),
            ("result", r"^was\s+kam\s+bei\s+(\S+)\s+raus\??$"),
        ):
            match = re.match(pattern, normalized, re.IGNORECASE)
            if match:
                return kind, match.group(1)
        return None

    def _known_aliases(self) -> dict[str, tuple[str, str]]:
        aliases: dict[str, tuple[str, str]] = {}
        for alias, path in self.config.repos.items():
            if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
                return {}
            folded = alias.casefold()
            if folded in aliases or not isinstance(path, (str, Path)) or not str(path).strip():
                return {}
            aliases[folded] = (alias, str(path))
        return aliases

    @staticmethod
    def _is_path_token(body: str, start: int, end: int) -> bool:
        if start and body[start - 1] in "/\\~":
            return True
        if start and body[start - 1] == "." and (start == 1 or not body[start - 2].isspace()):
            return True
        if end < len(body) and body[end] in "/\\":
            return True
        if end + 1 < len(body) and body[end] == "." and (
            body[end + 1].isalnum() or body[end + 1] in "_-"
        ):
            return True
        return False

    def _alias_matches(self, body: str) -> list[tuple[str, str, int, int]]:
        aliases = self._known_aliases()
        matches = []
        token_pattern = re.compile(r"(?<![\w-])([A-Za-z][A-Za-z0-9_-]*)(?![\w-])")
        for match in token_pattern.finditer(body):
            start, end = match.span(1)
            # Pfadsegmente wie ~/Mantis und /tmp/mantis sind keine Repo-Aliase.
            if self._is_path_token(body, start, end):
                continue
            alias = aliases.get(match.group(1).casefold())
            if alias:
                matches.append((alias[0], alias[1], start, end))
        return matches

    @staticmethod
    def _task_without_alias(body: str, start: int, end: int) -> str:
        task = body[:start] + body[end:]
        return task.strip(" \t\r\n,:;-")

    @staticmethod
    def _infer_mode(task: str) -> str | None:
        if _WRITE_WORDS.search(task):
            return "write"
        if _READ_WORDS.search(task):
            return "read"
        return None

    def _repo_root(self, raw_path: str) -> Path | None:
        try:
            repo = Path(raw_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not repo.is_dir():
            return None
        git = shutil.which("git")
        if not git:
            return None
        try:
            result = self._command_runner(
                [git, "-C", str(repo), "rev-parse", "--show-toplevel"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        try:
            git_root = Path(result.stdout.strip()).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        return repo if git_root == repo else None

    def _make_proposal(
        self,
        requester_id: str,
        dm_id: str,
        alias: str,
        raw_path: str,
        task: str,
        mode: str,
    ) -> Proposal | None:
        if mode not in {"read", "write"} or not task.strip():
            return None
        if not _MODEL_RE.fullmatch(self.config.model) or not _EFFORT_RE.fullmatch(self.config.effort):
            return None
        repo = self._repo_root(raw_path)
        if repo is None:
            return None
        branch = None
        worktree = None
        if mode == "write":
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
            suffix = uuid.uuid4().hex[:6]
            slug = _slug(task)
            branch = f"remote/{stamp}-{slug}-{suffix}"
            worktree = str(repo / ".worktrees" / f"remote-{stamp}-{slug}-{suffix}")
        now = self._clock()
        proposal = Proposal(
            proposal_id=uuid.uuid4().hex[:12],
            requester_id=requester_id,
            dm_id=dm_id,
            repo_alias=alias,
            repo_path=str(repo),
            task=task,
            mode=mode,
            model=self.config.model,
            effort=self.config.effort,
            created_at=now,
            expires_at=now + CONFIRMATION_TTL_SECONDS,
            branch=branch,
            worktree=worktree,
        )
        return replace(proposal, signature=self._proposal_signature(proposal))

    @staticmethod
    def _proposal_signature(proposal: Proposal) -> str:
        fields = (
            proposal.proposal_id,
            proposal.requester_id,
            proposal.dm_id,
            proposal.repo_alias,
            proposal.repo_path,
            proposal.task,
            proposal.mode,
            proposal.model,
            proposal.effort,
            proposal.created_at,
            proposal.expires_at,
            proposal.branch,
            proposal.worktree,
        )
        return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode("utf-8")).hexdigest()

    async def _persist(self, role: str, text: str) -> None:
        if self.persist is None:
            return
        try:
            self.persist(role, text, channel="discord", meta={"use_case": "agent"})
        except Exception:
            log.exception("Agent-Remote-Nachricht konnte nicht persistiert werden")

    async def _reply(self, route, text: str) -> None:
        await self._persist("assistant", text)
        if route is not None:
            try:
                await route.send(text)
            except Exception:
                log.warning("Agent-Remote-Antwort konnte nicht gesendet werden", exc_info=True)

    def _proposal_text(self, proposal: Proposal) -> str:
        target = proposal.worktree if proposal.mode == "write" else proposal.repo_path
        mode = "schreiben (workspace-write)" if proposal.mode == "write" else "lesen (read-only)"
        branch = f"Branch: {proposal.branch}\n" if proposal.branch else ""
        return (
            f"Vorschlag {proposal.proposal_id} · gültig 10 Minuten\n"
            f"Repo: {proposal.repo_alias}\n"
            f"Kanonischer Repo-Pfad: {proposal.repo_path}\n"
            f"Ziel: {target}\n"
            f"{branch}"
            f"Modus: {mode}\n"
            f"Modell: {proposal.model} · Effort: {proposal.effort}\n"
            "Auftrag (vollständiger Text):\n"
            f"{proposal.task}\n\n"
            "codex-task ergänzt beim Start seinen festen Worker-Präfix und gegebenenfalls "
            "Jev-Gedächtnisverweise. Schreiblauf: keine Commits, Merges oder Pushes.\n"
            "Zum Start antworte separat mit `ja` oder `start`; `nein` verwirft den Vorschlag."
        )

    async def _handle_start_request(self, msg, route, body: str) -> None:
        user_id = str(msg.sender_id)
        dm_id = str(msg.raw.channel.id)
        self.pending.pop(user_id, None)
        self.clarifications.pop(user_id, None)
        aliases = self._alias_matches(body)
        if len(aliases) != 1:
            self.clarifications[user_id] = Clarification(
                requester_id=user_id,
                dm_id=dm_id,
                body=body,
                repo_alias=None,
                task=None,
                stage="alias",
            )
            if not aliases:
                await self._reply(
                    route,
                    "Welches freigegebene Repo meinst du? Aktuell ist der Alias `mantis` erlaubt. "
                    "Pfadangaben im Chat werden nicht als Ziel verwendet.",
                )
            else:
                await self._reply(route, "Der Repo-Alias ist mehrdeutig. Bitte antworte mit genau einem Alias.")
            return
        alias, raw_path, start, end = aliases[0]
        task = self._task_without_alias(body, start, end)
        mode = self._infer_mode(task)
        if mode is None:
            self.clarifications[user_id] = Clarification(
                requester_id=user_id,
                dm_id=dm_id,
                body=body,
                repo_alias=alias,
                task=task,
                stage="mode",
            )
            await self._reply(route, "Soll Codex nur lesen oder Dateien schreiben? Antworte mit `lesen` oder `schreiben`.")
            return
        await self._create_and_send_proposal(msg, route, alias, raw_path, task, mode)

    async def _create_and_send_proposal(self, msg, route, alias, raw_path, task, mode) -> None:
        proposal = self._make_proposal(
            str(msg.sender_id), str(msg.raw.channel.id), alias, raw_path, task, mode,
        )
        if proposal is None:
            await self._reply(
                route,
                "Der Repo-Eintrag, Git-Root, Auftrag oder die Codex-Einstellung ist ungültig. "
                "Es wurde kein Lauf gestartet.",
            )
            return
        self.pending[proposal.requester_id] = proposal
        await self._reply(route, self._proposal_text(proposal))

    async def _handle_clarification(self, msg, route, text: str) -> bool:
        user_id = str(msg.sender_id)
        draft = self.clarifications.get(user_id)
        if draft is None:
            return False
        if draft.dm_id != str(msg.raw.channel.id):
            self.clarifications.pop(user_id, None)
            return True
        answer = text.strip().casefold()
        if draft.stage == "alias":
            aliases = self._known_aliases()
            resolved = aliases.get(answer)
            if resolved is None:
                self.clarifications.pop(user_id, None)
                await self._reply(route, "Unbekannter Repo-Alias. Bitte starte mit `codex: mantis …` neu.")
                return True
            task = self._task_without_alias(draft.body, *self._alias_span_for_answer(draft.body, answer))
            mode = self._infer_mode(task)
            if mode is None:
                self.clarifications[user_id] = Clarification(
                    requester_id=user_id,
                    dm_id=draft.dm_id,
                    body=draft.body,
                    repo_alias=resolved[0],
                    task=task,
                    stage="mode",
                )
                await self._reply(route, "Soll Codex nur lesen oder Dateien schreiben? Antworte mit `lesen` oder `schreiben`.")
                return True
            self.clarifications.pop(user_id, None)
            await self._create_and_send_proposal(msg, route, resolved[0], resolved[1], task, mode)
            return True
        if answer not in {"lesen", "schreiben"}:
            self.clarifications.pop(user_id, None)
            await self._reply(route, "Die Rückfrage wurde verworfen. Bitte starte den Auftrag erneut.")
            return True
        self.clarifications.pop(user_id, None)
        alias = draft.repo_alias
        path = self._known_aliases().get((alias or "").casefold())
        if path is None or draft.task is None:
            await self._reply(route, "Der Repo-Eintrag ist nicht mehr gültig. Bitte starte den Auftrag erneut.")
            return True
        await self._create_and_send_proposal(
            msg, route, path[0], path[1], draft.task, "read" if answer == "lesen" else "write",
        )
        return True

    @staticmethod
    def _alias_span_for_answer(body: str, answer: str) -> tuple[int, int]:
        for match in re.finditer(r"(?<![\w-])([A-Za-z][A-Za-z0-9_-]*)(?![\w-])", body):
            start, end = match.span(1)
            if AgentRemoteService._is_path_token(body, start, end):
                continue
            if match.group(1).casefold() == answer:
                return start, end
        return 0, 0

    async def _handle_confirmation(self, msg, route, text: str) -> bool:
        user_id = str(msg.sender_id)
        proposal = self.pending.get(user_id)
        if proposal is None:
            return False
        if proposal.dm_id != str(msg.raw.channel.id):
            self.pending.pop(user_id, None)
            return True
        message_time = self._message_created_at(msg)
        if message_time is not None and message_time < proposal.created_at:
            # Ein verspätet zugestelltes/dupliziertes altes "ja" darf keinen neueren
            # Vorschlag bestätigen und lässt dessen echtes Bestätigungsfenster offen.
            return True
        self.pending.pop(user_id, None)  # atomar verbrauchen, auch bei doppeltem Discord-Event
        self._consumed_confirmations[self._event_key(msg)] = self._clock() + CONFIRMATION_TTL_SECONDS
        if proposal.signature != self._proposal_signature(proposal):
            await self._reply(route, "Der Vorschlag wurde verändert und ist ungültig. Bitte starte ihn neu.")
            return True
        if self._clock() >= proposal.expires_at:
            await self._reply(route, "Der Vorschlag ist abgelaufen. Bitte sende den Auftrag erneut.")
            return True
        if text.strip().casefold() in _REJECTIONS:
            await self._reply(route, "Vorschlag verworfen.")
            return True
        current = self._known_aliases().get(proposal.repo_alias.casefold())
        current_root = self._repo_root(current[1]) if current else None
        if current_root is None or str(current_root) != proposal.repo_path:
            await self._reply(route, "Die Repo-Allowlist hat sich geändert. Bitte erstelle einen neuen Vorschlag.")
            return True
        await self._start_run(proposal, route)
        return True

    async def handle(self, msg: IncomingMessage, route=None) -> bool:
        """Verarbeitet Agent-Remote-Befehle vor dem LLM-Turn; sonst False."""
        if getattr(msg, "channel", None) != "discord":
            return False
        text = msg.text
        body = self._start_body(text)
        command = self._command_kind(text)
        user_id = str(msg.sender_id)
        pending = user_id in self.pending
        clarification = user_id in self.clarifications
        folded = text.strip().casefold()
        now = self._clock()
        self._consumed_confirmations = {
            key: expiry for key, expiry in self._consumed_confirmations.items() if expiry > now
        }
        if folded in _CONFIRMATIONS | _REJECTIONS and self._event_key(msg) in self._consumed_confirmations:
            return True
        is_confirmation = folded in _CONFIRMATIONS | _REJECTIONS and pending
        is_mode_reply = clarification and folded in {"lesen", "schreiben"}
        if body is None and command is None and not is_confirmation and not is_mode_reply:
            if self._is_trusted_dm(msg):
                self.pending.pop(user_id, None)
                self.clarifications.pop(user_id, None)
            return False
        if not self.config.enabled:
            if self._is_trusted_dm(msg):
                await self._persist("user", text)
                await self._reply(route, "Agent Remote ist deaktiviert.")
                return True
            return True
        if not self._is_trusted_dm(msg):
            self.pending.pop(user_id, None)
            self.clarifications.pop(user_id, None)
            return True
        await self._persist("user", text)
        if body is not None:
            await self._handle_start_request(msg, route, body)
            return True
        if is_mode_reply and await self._handle_clarification(msg, route, text):
            return True
        if is_confirmation and await self._handle_confirmation(msg, route, text):
            return True
        if command is not None:
            kind, value = command
            await self._handle_command(msg, route, kind, value)
            return True
        return True

    @staticmethod
    def _event_key(msg: IncomingMessage) -> tuple[str, str, str]:
        raw = msg.raw
        channel = getattr(raw, "channel", None)
        event_id = getattr(raw, "id", None)
        if event_id is None:
            event_id = id(raw)
        return str(msg.sender_id), str(getattr(channel, "id", "")), str(event_id)

    @staticmethod
    def _message_created_at(msg: IncomingMessage) -> float | None:
        raw = msg.raw
        created_at = getattr(raw, "created_at", None)
        if isinstance(created_at, datetime):
            return created_at.timestamp()
        event_id = getattr(raw, "id", None)
        if isinstance(event_id, int):
            return (event_id >> 22) / 1000 + 1420070400
        return None

    def _ensure_private_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.chmod(0o700)
        except OSError:
            pass

    def _record_path(self, remote_id: str) -> Path:
        if not _REMOTE_ID_RE.fullmatch(remote_id):
            raise ValueError("Ungültige Remote-ID")
        return self.runs_dir / remote_id / "state.json"

    def _save_run(self, record: RunRecord) -> None:
        path = self._record_path(record.remote_id)
        self._ensure_private_dir(self.state_dir)
        self._ensure_private_dir(self.runs_dir)
        self._ensure_private_dir(path.parent)
        payload = json.dumps(asdict(record), ensure_ascii=False, sort_keys=True)
        fd, temp_name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_name, path)
            path.chmod(0o600)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _load_runs(self) -> None:
        if not self.runs_dir.is_dir():
            return
        for folder in self.runs_dir.iterdir():
            state = folder / "state.json"
            if folder.is_symlink() or state.is_symlink() or not state.is_file():
                continue
            try:
                state.chmod(0o600)
                data = json.loads(state.read_text(encoding="utf-8"))
                record = RunRecord(**data)
                if record.remote_id != folder.name or not _REMOTE_ID_RE.fullmatch(record.remote_id):
                    continue
                self.runs[record.remote_id] = record
            except (OSError, ValueError, TypeError):
                log.warning("Ungültiger Agent-Remote-Status ignoriert: %s", folder.name)

    def _make_environment(self, tmp_dir: Path) -> dict[str, str]:
        bundle = Path(
            "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin"
        )
        paths = [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            str(self.home_dir / ".local" / "bin"),
            str(bundle),
        ]
        env = {
            "HOME": str(self.home_dir),
            "PATH": ":".join(paths),
            "TMPDIR": str(tmp_dir),
            "LANG": "en_US.UTF-8",
            "CODEX_TASK_MODEL": self.config.model,
            "CODEX_TASK_EFFORT": self.config.effort,
        }
        for name in ("USER", "LOGNAME"):
            value = os.environ.get(name)
            if value:
                env[name] = value
        return env

    def _create_worktree(self, proposal: Proposal) -> str:
        if proposal.worktree is None or proposal.branch is None:
            raise ValueError("Schreibvorschlag ohne isoliertes Ziel")
        repo = Path(proposal.repo_path).resolve(strict=True)
        parent = repo / ".worktrees"
        if parent.is_symlink():
            raise ValueError("Symlink-basiertes .worktrees-Verzeichnis wird abgelehnt")
        parent.mkdir(mode=0o700, exist_ok=True)
        resolved_parent = parent.resolve(strict=True)
        try:
            resolved_parent.relative_to(repo)
        except ValueError as exc:
            raise ValueError("Worktree-Ziel liegt außerhalb des Allowlist-Repos") from exc
        target = Path(proposal.worktree)
        if target.exists() or target.is_symlink():
            raise FileExistsError("Worktree-Ziel existiert bereits")
        git = shutil.which("git")
        if not git:
            raise FileNotFoundError("git nicht verfügbar")
        try:
            result = self._command_runner(
                [git, "-C", str(repo), "worktree", "add", "-b", proposal.branch, str(target), "main"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("Git konnte den Worktree nicht anlegen") from exc
        if result.returncode != 0 or not target.is_dir():
            raise RuntimeError("Git konnte den Worktree nicht abgezweigt von main anlegen")
        resolved_target = target.resolve(strict=True)
        try:
            resolved_target.relative_to(repo)
        except ValueError as exc:
            raise RuntimeError("Aufgelöster Worktree liegt außerhalb des Allowlist-Repos") from exc
        return str(resolved_target)

    def _recover_registry_before_pid_save(self, record: RunRecord) -> bool:
        """Schließt das Startfenster über TMPDIR/meta, ohne einen Auftrag neu zu starten."""
        for folder in self._registry_candidates(record):
            meta = _read_key_value_file(folder / "meta")
            try:
                pid = int(meta.get("pid", ""))
                run_path = Path(meta.get("run", "")).resolve(strict=True)
                run_cwd = Path(meta.get("dir", "")).resolve(strict=True)
                expected_cwd = Path(record.cwd).resolve(strict=True)
                tmp = Path(record.tmp_dir).resolve(strict=True)
                pgid = self._process_group(pid)
                identity = self._process_identity(pid)
            except (OSError, ValueError, RuntimeError):
                continue
            status = (_read_text(folder / "status", 64) or "").strip()
            if (
                run_path != folder.resolve(strict=True)
                or run_path.parent != tmp
                or run_cwd != expected_cwd
                or pgid != pid
                or not identity
                or status not in {"running", "ok", "fail", "quota"}
            ):
                continue
            record.pid = pid
            record.pgid = pgid
            record.process_identity = identity
            record.registry_id = folder.name
            record.registry_run = str(run_path)
            record.status = "running" if status == "running" else {
                "ok": "succeeded", "fail": "failed", "quota": "quota",
            }[status]
            if record.status != "running":
                record.ended_at = self._clock()
            self._save_run(record)
            return True
        return False

    def _task_prompt(self, task: str) -> str:
        return (
            f"{task}\n\n"
            "Zusätzliche Sicherheitsgrenze: keine Commits, Merges oder Pushes ausführen."
        )

    async def _start_run(self, proposal: Proposal, route) -> None:
        async with self._lock:
            if self.active_count >= min(MAX_REMOTE_RUNS, self.config.max_running):
                await self._reply(route, "Es laufen bereits zwei Agent-Remote-Prozesse.")
                return
            remote_id = "remote-" + uuid.uuid4().hex[:12]
            run_dir = self.runs_dir / remote_id
            tmp_dir = run_dir / "tmp"
            now = self._clock()
            record = RunRecord(
                remote_id=remote_id,
                requester_id=proposal.requester_id,
                dm_id=proposal.dm_id,
                repo_alias=proposal.repo_alias,
                repo_path=proposal.repo_path,
                mode=proposal.mode,
                model=proposal.model,
                effort=proposal.effort,
                status="preparing",
                created_at=now,
                deadline=now + min(DEFAULT_TIMEOUT_SECONDS, max(1, self.config.timeout_seconds)),
                tmp_dir=str(tmp_dir),
                cwd=proposal.worktree if proposal.mode == "write" else proposal.repo_path,
                branch=proposal.branch,
                worktree=proposal.worktree,
            )
            self.runs[remote_id] = record
            self._routes[remote_id] = route
            try:
                self._save_run(record)
            except OSError as exc:
                self.runs.pop(remote_id, None)
                self._routes.pop(remote_id, None)
                await self._reply(route, "Der persistente Laufstatus ist nicht beschreibbar; es wurde nichts gestartet.")
                log.warning("Agent-Remote-State konnte nicht angelegt werden (%s)", type(exc).__name__)
                return

        process = None
        monitor_started = False
        try:
            if proposal.mode == "write":
                record.cwd = self._create_worktree(proposal)
                record.worktree = record.cwd
            tmp_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            tmp_dir.chmod(0o700)
            if self._process_identity(os.getpid()) is None:
                raise RuntimeError("Prozessidentität kann nicht sicher geprüft werden")
            binary = str(Path(self.config.task_binary).expanduser().resolve())
            args = [binary, "-C", record.cwd, "-m", proposal.model, "-e", proposal.effort]
            if proposal.mode == "read":
                args.append("-r")
            args.append(self._task_prompt(proposal.task))
            process = self._popen(
                args,
                cwd=record.cwd,
                env=self._make_environment(tmp_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                umask=0o077,
                shell=False,
            )
            record.pid = int(process.pid)
            record.pgid = int(process.pid)
            record.process_identity = self._process_identity(record.pid)
            record.deadline = self._clock() + min(
                DEFAULT_TIMEOUT_SECONDS, max(1, self.config.timeout_seconds),
            )
            record.status = "running" if process.poll() is None else "failed"
            if record.status == "failed":
                record.ended_at = self._clock()
            try:
                self._save_run(record)
            except OSError as exc:
                log.warning("Agent-Remote-PID konnte nicht persistiert werden (%s)", type(exc).__name__)
            self._sync_registry(record)
            if record.status == "running":
                self._track(asyncio.create_task(self._monitor_process(remote_id, process)))
                monitor_started = True
                reg = f" · Registry {record.registry_id}" if record.registry_id else ""
                await self._reply(route, f"Lauf {remote_id} gestartet{reg}. Status wird gemeldet.")
            else:
                await self._finish_run(record, int(process.returncode or 1))
        except Exception as exc:
            if process is not None and process.poll() is None:
                record.pid = int(process.pid)
                record.pgid = int(process.pid)
                if record.process_identity is None:
                    record.process_identity = self._process_identity(record.pid)
                record.status = "running"
                try:
                    self._save_run(record)
                except OSError:
                    log.error("Agent-Remote-PID bleibt nur über TMPDIR/meta wiederauffindbar")
                if not monitor_started:
                    self._track(asyncio.create_task(self._monitor_process(remote_id, process)))
                log.warning("Nach dem codex-task-Start trat ein Folgefehler auf (%s)", type(exc).__name__)
                await self._reply(route, f"Lauf {remote_id} gestartet; die Statusverknüpfung wird wiederhergestellt.")
                return
            record.status = "failed"
            record.ended_at = self._clock()
            try:
                self._save_run(record)
            except OSError:
                log.error("Agent-Remote-Fehlerstatus konnte nicht persistiert werden")
            log.warning("Agent-Remote-Start fehlgeschlagen (%s)", type(exc).__name__)
            await self._reply(route, f"Lauf konnte nicht gestartet werden ({type(exc).__name__}).")

    def _track(self, task: asyncio.Task) -> None:
        self._tasks.add(task)

        def done(completed: asyncio.Task) -> None:
            self._tasks.discard(completed)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                log.warning("Agent-Remote-Hintergrundtask fehlgeschlagen (%s)", type(error).__name__)

        task.add_done_callback(done)

    def _registry_candidates(self, record: RunRecord) -> list[Path]:
        tmp_dir = Path(record.tmp_dir)
        try:
            resolved_tmp = tmp_dir.resolve(strict=True)
            record_dir = self._record_path(record.remote_id).parent.resolve(strict=True)
            resolved_tmp.relative_to(record_dir)
        except (OSError, ValueError):
            return []
        try:
            return [
                item for item in resolved_tmp.glob("codex-task.*")
                if item.is_dir() and not item.is_symlink()
            ]
        except OSError:
            return []

    def _sync_registry(self, record: RunRecord) -> dict[str, str]:
        for folder in self._registry_candidates(record):
            meta = _read_key_value_file(folder / "meta")
            try:
                meta_pid = int(meta.get("pid", ""))
                run_path = Path(meta.get("run", "")).resolve(strict=True)
                tmp = Path(record.tmp_dir).resolve(strict=True)
                repo_path = Path(meta.get("dir", "")).resolve(strict=True)
            except (OSError, ValueError, RuntimeError):
                continue
            if (
                meta_pid != record.pid
                or run_path != folder.resolve(strict=True)
                or run_path.parent != tmp
                or repo_path != Path(record.cwd).resolve(strict=True)
            ):
                continue
            record.registry_id = folder.name
            record.registry_run = str(run_path)
            self._save_run(record)
            status = _read_text(folder / "status", 64)
            return {**meta, "status": (status or "").strip()}
        return {}

    def _registry_state(self, record: RunRecord) -> str:
        metadata = self._sync_registry(record)
        return metadata.get("status", "")

    async def _monitor_process(self, remote_id: str, process) -> None:
        record = self.runs.get(remote_id)
        if record is None:
            return
        while record.status == "running" and process.poll() is None:
            if self._clock() >= record.deadline:
                terminated = await self._terminate(record, process)
                if not terminated and process.poll() is not None:
                    await self._finish_run(record, int(process.returncode or 0))
                    return
                record.status = "timed_out" if terminated else "interrupted"
                record.ended_at = self._clock()
                self._save_run(record)
                await self._notify_completion(record)
                return
            self._sync_registry(record)
            await self._sleep(min(1.0, max(0.05, record.deadline - self._clock())))
        if record.status != "running":
            return
        await self._finish_run(record, int(process.returncode or 0))

    async def _monitor_recovered(self, remote_id: str) -> None:
        record = self.runs.get(remote_id)
        if record is None:
            return
        while self._identity_matches(record.pid, record.pgid, record.process_identity):
            if self._clock() >= record.deadline:
                terminated = await self._terminate(record)
                record.status = "timed_out" if terminated else "interrupted"
                record.ended_at = self._clock()
                self._save_run(record)
                await self._notify_completion(record)
                return
            await self._sleep(1.0)
        self._finish_from_registry(record)
        if record.status != "running":
            await self._notify_completion(record)

    def _identity_matches(self, pid, pgid, identity) -> bool:
        if not isinstance(pid, int) or not isinstance(pgid, int) or not identity:
            return False
        try:
            return pgid == pid and self._process_group(pid) == pgid and self._process_identity(pid) == identity
        except (OSError, ProcessLookupError, PermissionError):
            return False

    async def _terminate(self, record: RunRecord, process=None) -> bool:
        return await self._terminate_identity(
            record.pid, record.pgid, record.process_identity, process,
        )

    async def _terminate_identity(self, pid, pgid, identity, process=None) -> bool:
        if not self._identity_matches(pid, pgid, identity):
            return False
        if not self._signal_process_group(pid, pgid, "TERM", identity):
            return False
        deadline = self._clock() + TERMINATION_GRACE_SECONDS
        while self._clock() < deadline:
            if process is not None and process.poll() is not None:
                return True
            if not self._identity_matches(pid, pgid, identity):
                return True
            await self._sleep(min(0.1, max(0.01, deadline - self._clock())))
        if self._identity_matches(pid, pgid, identity):
            return self._signal_process_group(pid, pgid, "KILL", identity)
        return True

    async def _finish_run(self, record: RunRecord, return_code: int) -> None:
        registry_status = self._registry_state(record)
        record.status = {
            "ok": "succeeded",
            "fail": "failed",
            "quota": "quota",
            "running": "interrupted",
        }.get(
            registry_status,
            "quota" if return_code == 75 else "interrupted" if return_code == 0 else "failed",
        )
        record.ended_at = self._clock()
        self._save_run(record)
        await self._notify_completion(record)

    def _finish_from_registry(self, record: RunRecord) -> None:
        status = self._registry_state(record)
        record.status = {
            "ok": "succeeded",
            "fail": "failed",
            "quota": "quota",
        }.get(status, "interrupted")
        record.ended_at = self._clock()
        self._save_run(record)

    async def _notify_completion(self, record: RunRecord) -> None:
        if record.notification_sent_at is not None or record.status == "cancelled":
            return
        route = self._route_for_record(record)
        if route is None:
            return
        try:
            await route.send(self._completion_text(record))
        except Exception:
            log.warning("Agent-Remote-Abschlussmeldung konnte nicht gesendet werden", exc_info=True)
            self._queue_notification_retry(record.remote_id)
            return
        await self._persist("assistant", self._completion_text(record))
        record.notification_sent_at = self._clock()
        self._save_run(record)

    def _route_for_record(self, record: RunRecord):
        route = self._routes.get(record.remote_id)
        if route is None and self.discord_channel is not None:
            route_factory = getattr(self.discord_channel, "for_user", None)
            if route_factory:
                route = route_factory(record.requester_id)
        return route

    @staticmethod
    def _completion_text(record: RunRecord) -> str:
        labels = {
            "succeeded": "abgeschlossen",
            "failed": "fehlgeschlagen",
            "quota": "Kontingent erschöpft",
            "timed_out": "Zeitlimit erreicht",
            "interrupted": "unterbrochen",
        }
        return (
            f"Agent-Remote {record.remote_id}: {labels.get(record.status, record.status)}. "
            "Ergebnis und Diff kannst du mit der Lauf-ID abrufen."
        )

    def _queue_notification_retry(self, remote_id: str) -> None:
        if remote_id in self._notification_retrying:
            return
        self._notification_retrying.add(remote_id)
        self._track(asyncio.create_task(self._retry_notification(remote_id)))

    async def _retry_notification(self, remote_id: str) -> None:
        try:
            for _attempt in range(6):
                await self._sleep(5.0)
                record = self.runs.get(remote_id)
                if record is None or record.notification_sent_at is not None:
                    return
                route = self._route_for_record(record)
                if route is None:
                    continue
                try:
                    await route.send(self._completion_text(record))
                except Exception:
                    continue
                await self._persist("assistant", self._completion_text(record))
                record.notification_sent_at = self._clock()
                self._save_run(record)
                return
        finally:
            self._notification_retrying.discard(remote_id)

    async def start(self) -> None:
        """Stellt Statusdateien wieder her; laufende Prozesse werden nie neu gestartet."""
        if not self.config.enabled:
            return
        for record in list(self.runs.values()):
            if record.status == "cancelling":
                if await self._terminate(record):
                    record.status = "cancelled"
                    record.ended_at = self._clock()
                    self._save_run(record)
                else:
                    self._finish_from_registry(record)
            if record.test_status == "cancelling":
                if await self._terminate_identity(
                    record.test_pid, record.test_pgid, record.test_identity,
                ):
                    record.test_status = "cancelled"
                else:
                    record.test_status = "interrupted"
                self._save_run(record)
            if record.status == "preparing":
                recovered = self._recover_registry_before_pid_save(record)
                if recovered and record.status == "running":
                    self._track(asyncio.create_task(self._monitor_recovered(record.remote_id)))
                elif recovered:
                    pass
                elif record.pid:
                    self._finish_from_registry(record)
                else:
                    record.status = "interrupted"
                    record.ended_at = self._clock()
                    self._save_run(record)
            if record.status == "running":
                self._sync_registry(record)
                if self._identity_matches(record.pid, record.pgid, record.process_identity):
                    self._track(asyncio.create_task(self._monitor_recovered(record.remote_id)))
                else:
                    self._finish_from_registry(record)
            if record.status == "running" and record.test_status == "running":
                self._track(asyncio.create_task(self._monitor_recovered_test(record.remote_id)))
            if record.status not in {"running", "preparing"}:
                await self._notify_completion(record)

    async def stop(self) -> None:
        """Beendet nur die Aufsichtstasks; codex-task-Prozesse laufen eigenständig weiter."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _handle_command(self, msg, route, kind: str, value: str | None) -> None:
        if kind == "list":
            await self._list_runs(route)
            return
        if kind == "latest":
            completed = [
                run for run in self.runs.values()
                if run.requester_id == str(msg.sender_id) and run.ended_at is not None
            ]
            if not completed:
                await self._reply(route, "Für dich ist kein beendeter Agent-Remote-Lauf gespeichert.")
                return
            record = max(completed, key=lambda run: run.ended_at or run.created_at)
            await self._show_result(route, record.remote_id)
            return
        record = self._find_remote(value or "")
        if record is not None and record.requester_id != str(msg.sender_id):
            await self._reply(route, "Diese Lauf-ID ist nicht verfügbar.")
            return
        if kind == "cancel":
            if record is None:
                await self._reply(route, "Diese ID gehört zu keinem abbrechbaren Agent-Remote-Lauf.")
                return
            await self._cancel(record, route)
        elif kind == "diff":
            if record is None:
                await self._reply(route, "Zu dieser ID gibt es keinen Remote-Worktree-Diff.")
                return
            await self._show_diff(route, record)
        elif kind == "tests":
            if record is None:
                await self._reply(route, "Tests sind nur für eine bekannte Remote-ID verfügbar.")
                return
            await self._start_tests(route, record)
        elif kind == "result":
            if record is not None:
                await self._show_result(route, record.remote_id)
            else:
                await self._show_local_result(route, value or "")

    def _find_remote(self, identifier: str) -> RunRecord | None:
        if not _REMOTE_ID_RE.fullmatch(identifier) and not _CODEX_ID_RE.fullmatch(identifier):
            return None
        return next(
            (run for run in self.runs.values() if identifier in {run.remote_id, run.registry_id}),
            None,
        )

    async def _list_runs(self, route) -> None:
        binary = str(Path(self.config.ps_binary).expanduser())
        lines: list[str] = []
        env = self._make_environment(self.state_dir)
        try:
            result = self._command_runner(
                [binary],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
                cwd=str(self.home_dir),
                env=env,
                shell=False,
            )
            if result.stdout.strip():
                lines.append("Rechnerläufe (codex-ps):\n" + result.stdout.strip())
            elif result.returncode:
                lines.append("Rechnerläufe: codex-ps meldet einen Fehler.")
        except (OSError, subprocess.SubprocessError):
            lines.append("Rechnerläufe: codex-ps ist nicht verfügbar.")
        lines.extend(self._list_local_registry_ids())
        if self.runs:
            lines.append("Agent-Remote-Läufe:")
            for run in sorted(self.runs.values(), key=lambda item: item.created_at, reverse=True):
                registry = f" ({run.registry_id})" if run.registry_id else ""
                lines.append(f"{run.remote_id}{registry} · {run.status} · {run.repo_alias} · {run.mode}")
        await self._reply(route, "\n".join(lines) or "Keine registrierten Codex-Läufe gefunden.")

    def _list_local_registry_ids(self) -> list[str]:
        registry_dir = self.home_dir / ".local" / "state" / "codex-task" / "runs"
        if not registry_dir.is_dir():
            return []
        output = ["Registrierte Rechner-IDs:"]
        for folder in sorted(registry_dir.iterdir()):
            if folder.is_symlink() or not folder.is_dir():
                continue
            identifier = folder.name
            if not _CODEX_ID_RE.fullmatch(identifier):
                continue
            meta = _read_key_value_file(folder / "meta")
            status = (_read_text(folder / "status", 64) or "unbekannt").strip()
            title = meta.get("title", "").replace("@", "＠")[:100]
            suffix = f" · {title}" if title else ""
            output.append(f"{identifier} · {status}{suffix}")
        return output if len(output) > 1 else []

    async def _show_result(self, route, remote_id: str) -> None:
        record = self.runs.get(remote_id)
        if record is None:
            await self._reply(route, "Diese Lauf-ID ist nicht verfügbar.")
            return
        if record.status in {"running", "preparing"}:
            await self._reply(route, f"{record.remote_id}: läuft seit {int(self._clock() - record.created_at)} Sekunden.")
            return
        result = None
        if record.registry_run and record.registry_id:
            try:
                registry_run = Path(record.registry_run)
                resolved_run = registry_run.resolve(strict=True)
                resolved_tmp = Path(record.tmp_dir).resolve(strict=True)
                output = resolved_run / "last.txt"
                if (
                    _CODEX_ID_RE.fullmatch(record.registry_id)
                    and not registry_run.is_symlink()
                    and resolved_run.name == record.registry_id
                    and resolved_run.parent == resolved_tmp
                    and not output.is_symlink()
                ):
                    result = _read_text(output)
            except (OSError, RuntimeError):
                result = None
        if result is None:
            await self._reply(route, f"{record.remote_id}: {record.status}; es liegt keine Antwortdatei vor.")
            return
        await self._reply(route, result)

    async def _show_local_result(self, route, identifier: str) -> None:
        if not _CODEX_ID_RE.fullmatch(identifier):
            await self._reply(route, "Bitte nenne eine eindeutige Remote- oder codex-task-ID.")
            return
        registry = self.home_dir / ".local" / "state" / "codex-task" / "runs" / identifier
        if registry.is_symlink() or not registry.is_dir():
            await self._reply(route, "Diese lokale Lauf-ID ist nicht verfügbar.")
            return
        meta = _read_key_value_file(registry / "meta")
        try:
            raw_run_dir = Path(meta["run"])
            if raw_run_dir.is_symlink():
                raise ValueError
            run_dir = raw_run_dir.resolve(strict=True)
            if (
                not run_dir.is_dir()
                or run_dir.name != identifier
                or run_dir.is_symlink()
                or (run_dir / "last.txt").is_symlink()
            ):
                raise ValueError
        except (KeyError, OSError, ValueError, RuntimeError):
            await self._reply(route, "Für diesen lokalen Lauf gibt es keine sichere Ergebnisdatei.")
            return
        result = _read_text(run_dir / "last.txt")
        status = _read_text(registry / "status", 64)
        await self._reply(route, result or f"{identifier}: {(status or 'unbekannt').strip()}.")

    async def _show_diff(self, route, record: RunRecord) -> None:
        if not record.worktree:
            await self._reply(route, "Für diesen Leselauf existiert kein Remote-Worktree-Diff.")
            return
        worktree = Path(record.worktree)
        if not self._valid_worktree(record, worktree):
            await self._reply(route, "Der gespeicherte Worktree-Pfad ist nicht mehr gültig.")
            return
        git = shutil.which("git")
        if not git:
            await self._reply(route, "git ist nicht verfügbar.")
            return
        try:
            result = self._command_runner(
                [git, "-C", str(worktree), "diff", "--stat"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            await self._reply(route, "Diff konnte nicht gelesen werden.")
            return
        output = result.stdout.strip() or "Keine Änderungen im git diff."
        if record.test_output:
            output += f"\nLetzte lokale Testausgabe: {record.test_output}"
        await self._reply(route, f"{record.remote_id}\n{output}")

    def _valid_worktree(self, record: RunRecord, worktree: Path) -> bool:
        try:
            root = Path(record.repo_path).resolve(strict=True)
            target = worktree.resolve(strict=True)
            target.relative_to(root / ".worktrees")
            return target.is_dir() and record.mode == "write"
        except (OSError, ValueError, RuntimeError):
            return False

    async def _cancel(self, record: RunRecord, route) -> None:
        if record.status == "running" and record.pid and record.process_identity:
            record.status = "cancelling"
            self._save_run(record)
            if not await self._terminate(record):
                self._finish_from_registry(record)
                await self._reply(route, f"Lauf {record.remote_id} ließ sich nicht sicher signalisieren.")
                return
            record.status = "cancelled"
            record.ended_at = self._clock()
            self._save_run(record)
            await self._reply(route, f"Lauf {record.remote_id} wurde abgebrochen.")
            return
        if record.test_status == "running" and record.test_pid and record.test_identity:
            record.test_status = "cancelling"
            self._save_run(record)
            if not await self._terminate_identity(
                record.test_pid, record.test_pgid, record.test_identity,
            ):
                await self._reply(route, f"Testlauf für {record.remote_id} ließ sich nicht sicher signalisieren.")
                return
            record.test_status = "cancelled"
            self._save_test_output(record)
            self._save_run(record)
            await self._reply(route, f"Testlauf für {record.remote_id} wurde abgebrochen.")
            return
        await self._reply(route, f"Lauf {record.remote_id} läuft nicht mehr.")

    async def _start_tests(self, route, record: RunRecord) -> None:
        vector = self.config.test_args.get(record.repo_alias)
        if (
            not isinstance(vector, (list, tuple))
            or not vector
            or not all(isinstance(part, str) and part for part in vector)
        ):
            await self._reply(route, "Für dieses Repo ist kein vertrauenswürdiger Test-Argumentvektor konfiguriert.")
            return
        if record.status != "succeeded" or not record.worktree or not self._valid_worktree(
            record, Path(record.worktree),
        ):
            await self._reply(route, "Tests sind nur für einen abgeschlossenen Remote-Worktree verfügbar.")
            return
        async with self._lock:
            if self.active_count >= min(MAX_REMOTE_RUNS, self.config.max_running):
                await self._reply(route, "Es laufen bereits zwei Agent-Remote-Prozesse.")
                return
            record.test_status = "preparing"
            record.test_deadline = self._clock() + min(
                DEFAULT_TIMEOUT_SECONDS, max(1, self.config.timeout_seconds),
            )
            test_file = Path(record.tmp_dir).parent / "tests.log"
            try:
                self._save_run(record)
            except OSError as exc:
                record.test_status = None
                await self._reply(route, "Der persistente Teststatus ist nicht beschreibbar; Tests wurden nicht gestartet.")
                log.warning("Agent-Remote-Teststatus konnte nicht gespeichert werden (%s)", type(exc).__name__)
                return
        process = None
        monitor_started = False
        try:
            if self._process_identity(os.getpid()) is None:
                raise RuntimeError("Prozessidentität kann nicht sicher geprüft werden")
            output = test_file.open("ab")
            try:
                process = self._popen(
                    list(vector),
                    cwd=record.worktree,
                    env=self._make_environment(Path(record.tmp_dir)),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                    umask=0o077,
                    shell=False,
                )
            finally:
                output.close()
            record.test_pid = int(process.pid)
            record.test_pgid = int(process.pid)
            record.test_identity = self._process_identity(record.test_pid)
            record.test_status = "running" if process.poll() is None else "failed"
            try:
                self._save_run(record)
            except OSError as exc:
                log.warning("Agent-Remote-Test-PID konnte nicht persistiert werden (%s)", type(exc).__name__)
            if record.test_status == "running":
                self._track(asyncio.create_task(self._monitor_test_process(record.remote_id, process)))
                monitor_started = True
                await self._reply(route, f"Lokaler Testlauf für {record.remote_id} gestartet.")
            else:
                await self._finish_test(record, int(process.returncode or 1))
        except Exception as exc:
            if process is not None and process.poll() is None:
                record.test_pid = int(process.pid)
                record.test_pgid = int(process.pid)
                if record.test_identity is None:
                    record.test_identity = self._process_identity(record.test_pid)
                record.test_status = "running"
                try:
                    self._save_run(record)
                except OSError:
                    log.error("Agent-Remote-Test-PID bleibt nicht persistent gespeichert")
                if not monitor_started:
                    self._track(asyncio.create_task(self._monitor_test_process(record.remote_id, process)))
                await self._reply(route, f"Lokaler Testlauf für {record.remote_id} gestartet; Status folgt.")
                return
            record.test_status = "failed"
            try:
                self._save_run(record)
            except OSError:
                log.error("Agent-Remote-Testfehler konnte nicht persistiert werden")
            log.warning("Agent-Remote-Teststart fehlgeschlagen (%s)", type(exc).__name__)
            await self._reply(route, "Der lokale Testlauf konnte nicht gestartet werden.")

    async def _monitor_test_process(self, remote_id: str, process) -> None:
        record = self.runs.get(remote_id)
        if record is None:
            return
        while record.test_status == "running" and process.poll() is None:
            if record.test_deadline is not None and self._clock() >= record.test_deadline:
                terminated = await self._terminate_identity(
                    record.test_pid, record.test_pgid, record.test_identity, process,
                )
                if not terminated and process.poll() is not None:
                    await self._finish_test(record, int(process.returncode or 0))
                    return
                record.test_status = "timed_out" if terminated else "interrupted"
                record.test_exit_code = None
                self._save_test_output(record)
                self._save_run(record)
                return
            await self._sleep(1.0)
        if record.test_status != "running":
            return
        await self._finish_test(record, int(process.returncode or 0))

    async def _monitor_recovered_test(self, remote_id: str) -> None:
        record = self.runs.get(remote_id)
        if record is None:
            return
        while self._identity_matches(record.test_pid, record.test_pgid, record.test_identity):
            if record.test_deadline is not None and self._clock() >= record.test_deadline:
                terminated = await self._terminate_identity(
                    record.test_pid, record.test_pgid, record.test_identity,
                )
                record.test_status = "timed_out" if terminated else "interrupted"
                self._save_test_output(record)
                self._save_run(record)
                return
            await self._sleep(1.0)
        record.test_status = "interrupted"
        self._save_test_output(record)
        self._save_run(record)

    def _save_test_output(self, record: RunRecord) -> None:
        path = Path(record.tmp_dir).parent / "tests.log"
        content = _read_text(path, 8192)
        if content:
            record.test_output = next((line.strip() for line in reversed(content.splitlines()) if line.strip()), None)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    async def _finish_test(self, record: RunRecord, return_code: int) -> None:
        record.test_status = "succeeded" if return_code == 0 else "failed"
        record.test_exit_code = return_code
        self._save_test_output(record)
        self._save_run(record)
        if record.test_status == "succeeded":
            result = "bestanden"
        else:
            result = "fehlgeschlagen"
        route = self._routes.get(record.remote_id)
        if route is not None:
            await self._reply(route, f"Lokaler Pytest-Lauf für {record.remote_id}: {result}.")

