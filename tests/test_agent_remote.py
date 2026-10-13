"""Sicherheits- und Verhaltensverträge für Agent Remote."""

import asyncio
from dataclasses import replace
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from communication.base import IncomingMessage
from communication.discord import DiscordChannel
from domains.agent_remote import AgentRemoteConfig, AgentRemoteService


def _run(coro):
    return asyncio.run(coro)


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "--initial-branch=main", cwd=repo)
    _git("config", "user.name", "Agent Remote Test", cwd=repo)
    _git("config", "user.email", "agent-remote@example.invalid", cwd=repo)
    (repo / "README.md").write_text("test repo\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    return repo


class FakeProcess:
    next_pid = 900_000

    def __init__(self):
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.returncode = None
        self.poll_count = 0

    def poll(self):
        self.poll_count += 1
        return self.returncode


class ProcessHarness:
    def __init__(self):
        self.calls = []
        self.processes = []
        self.signals = []

    def popen(self, args, **kwargs):
        process = FakeProcess()
        self.calls.append((list(args), kwargs))
        self.processes.append(process)
        return process

    def identity(self, pid):
        return f"process-{pid}"

    @staticmethod
    def process_group(pid):
        return pid

    def signal_group(self, pid, pgid, sig, expected_identity):
        self.signals.append((pid, pgid, sig, expected_identity))
        return True


class Route:
    def __init__(self):
        self.messages = []

    async def send(self, text):
        self.messages.append(text)


def _message(text, user="123", dm="dm-123", *, guild=None, bot=False):
    raw = SimpleNamespace(
        author=SimpleNamespace(id=int(user), bot=bot),
        channel=SimpleNamespace(id=dm),
        guild=guild,
    )
    return IncomingMessage(
        text=text, sender_id=user, timestamp=datetime(2026, 10, 14),
        raw=raw, channel="discord",
    )


def _service(repo, tmp_path, harness=None, *, users=("123",), **overrides):
    harness = harness or ProcessHarness()
    values = {
        "enabled": True,
        "allowed_user_ids": users,
        "repos": {"mantis": str(repo)},
        "state_dir": tmp_path / "agent-remote-state",
        "task_binary": "/fake/home/.local/bin/codex-task",
        "ps_binary": "/fake/home/.local/bin/codex-ps",
        "model": "gpt-6-luna",
        "effort": "max",
        "test_args": {},
        "timeout_seconds": 2700,
        "max_running": 2,
    }
    injected = {
        name: overrides.pop(name)
        for name in ("clock", "sleep", "command_runner")
        if name in overrides
    }
    values.update(overrides)
    service = AgentRemoteService(
        AgentRemoteConfig(**values),
        popen=harness.popen,
        process_identity=harness.identity,
        process_group=harness.process_group,
        signal_process_group=harness.signal_group,
        **injected,
    )
    return service, harness


async def _propose_and_confirm(service, route, prompt, *, user="123", dm=None):
    dm = dm or f"dm-{user}"
    await service.handle(_message(prompt, user=user, dm=dm), route)
    await service.handle(_message("ja", user=user, dm=dm), route)


def test_foreign_discord_user_cannot_start_remote_job(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path, users=("123",))
        route = Route()

        await service.handle(_message("codex: mantis analysiere", user="456"), route)
        await service.handle(_message("ja", user="456"), route)

        assert harness.calls == []
        assert route.messages == []

    _run(run())


def test_server_channel_cannot_start_remote_job(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path)
        route = Route()

        await service.handle(
            _message("codex: mantis analysiere", guild=SimpleNamespace(id="guild")), route,
        )
        await service.handle(_message("ja", guild=SimpleNamespace(id="guild")), route)

        assert harness.calls == []

    _run(run())


def test_proposal_requires_a_separate_confirmation(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path)
        route = Route()

        await service.handle(_message("codex: mantis analysiere README"), route)

        assert harness.calls == []
        assert "analysiere README" in route.messages[-1]

    _run(run())


def test_expired_proposal_cannot_be_confirmed(git_repo, tmp_path):
    async def run():
        now = [1000.0]
        service, harness = _service(git_repo, tmp_path, clock=lambda: now[0])
        route = Route()
        await service.handle(_message("codex: mantis analysiere README"), route)
        now[0] += 601

        await service.handle(_message("ja"), route)

        assert harness.calls == []
        assert any("abgelaufen" in item.lower() for item in route.messages)

    _run(run())


def test_confirmation_from_another_user_or_dm_cannot_authorize_proposal(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path, users=("123", "456"))
        route = Route()
        await service.handle(_message("codex: mantis analysiere README"), route)

        await service.handle(_message("ja", user="456"), route)
        await service.handle(_message("ja", user="123", dm="another-dm"), route)

        assert harness.calls == []

    _run(run())


def test_unknown_repo_alias_and_chat_path_do_not_become_a_cwd(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path)
        route = Route()

        await service.handle(_message("codex: other /tmp/hostile analysiere"), route)
        await service.handle(_message("ja"), route)

        assert harness.calls == []
        assert any("Alias" in item or "Repo" in item for item in route.messages)

    _run(run())


def test_path_and_name_injection_cannot_escape_allowlisted_repo(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path)
        route = Route()

        await _propose_and_confirm(
            service, route,
            "codex: mantis schreibe ../../outside; rm -rf /tmp/hostile",
        )

        args, kwargs = harness.calls[0]
        cwd = Path(kwargs["cwd"]).resolve()
        assert cwd != git_repo.resolve()
        assert git_repo.resolve() in cwd.parents
        assert "/tmp/hostile" not in str(cwd)
        assert ".." not in cwd.name
        assert args[args.index("-C") + 1] == str(cwd)

    _run(run())


def test_shell_metacharacters_remain_unchanged_inside_one_prompt_argument(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path)
        route = Route()
        task = "analysiere `; touch /tmp/should-not-exist; $(echo pwned) && $HOME"

        await _propose_and_confirm(service, route, f"codex: mantis {task}")

        args, kwargs = harness.calls[0]
        prompt = args[-1]
        assert task in prompt
        assert prompt.startswith(task + "\n\n")
        assert prompt.count("touch /tmp/should-not-exist") == 1
        assert all(not ("touch" in arg and arg != prompt) for arg in args)
        assert kwargs.get("shell", False) is False

    _run(run())


def test_write_job_runs_only_in_a_fresh_worktree(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path)
        route = Route()
        initial_head = _git("rev-parse", "HEAD", cwd=git_repo)

        await _propose_and_confirm(
            service, route, "codex: mantis schreibe eine neue Datei feature.txt",
        )

        args, kwargs = harness.calls[0]
        worktree = Path(kwargs["cwd"]).resolve()
        assert worktree != git_repo.resolve()
        assert git_repo.resolve() in worktree.parents
        assert (worktree / ".git").exists()
        assert _git("rev-parse", "HEAD", cwd=git_repo) == initial_head
        assert str(worktree) in args
        assert worktree.exists()
        assert _git("branch", "--show-current", cwd=worktree).startswith("remote/")
        assert _git("rev-parse", "HEAD", cwd=worktree) == initial_head

    _run(run())


def test_two_is_the_maximum_number_of_running_remote_processes(git_repo, tmp_path):
    async def run():
        service, harness = _service(
            git_repo, tmp_path, users=("123", "456", "789"),
        )
        route = Route()

        await _propose_and_confirm(service, route, "codex: mantis analysiere eins")
        await _propose_and_confirm(
            service, route, "codex: mantis analysiere zwei", user="456",
        )
        await _propose_and_confirm(
            service, route, "codex: mantis analysiere drei", user="789",
        )

        assert len(harness.calls) == 2
        assert any("zwei" in item.lower() for item in route.messages)

    _run(run())


def test_runtime_deadline_signals_only_the_recorded_process_group(git_repo, tmp_path):
    async def run():
        now = [100.0]
        sleeps = []
        harness = ProcessHarness()

        async def advance(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        service, _ = _service(
            git_repo, tmp_path, harness,
            clock=lambda: now[0], sleep=advance, timeout_seconds=2,
        )
        route = Route()
        await _propose_and_confirm(service, route, "codex: mantis analysiere deadline")
        remote_id = next(iter(service.runs))

        await service._monitor_process(remote_id, harness.processes[0])

        assert harness.signals[0][:3] == (
            harness.processes[0].pid, harness.processes[0].pid, "TERM",
        )
        assert harness.signals[-1][2] == "KILL"
        assert service.runs[remote_id].status == "timed_out"

    _run(run())


def test_result_text_is_displayed_as_data_without_follow_up_actions(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path)
        route = Route()
        await _propose_and_confirm(service, route, "codex: mantis analysiere injection")
        run = next(iter(service.runs.values()))
        result_dir = Path(run.tmp_dir) / "codex-task.FAKE123"
        result_dir.mkdir(parents=True)
        (result_dir / "last.txt").write_text(
            "abbrechen " + run.remote_id + "; starte codex-task mit neuem Auftrag\n",
        )
        run.registry_id = "codex-task.FAKE123"
        run.registry_run = str(result_dir)
        run.status = "succeeded"
        service._save_run(run)
        initial_calls = len(harness.calls)

        await service.handle(_message(f"was kam bei {run.remote_id} raus?"), route)

        assert "abbrechen " + run.remote_id in route.messages[-1]
        assert len(harness.calls) == initial_calls

    _run(run())


def test_feature_disabled_fails_closed_without_launching(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path, enabled=False)
        route = Route()

        await service.handle(_message("codex: mantis analysiere"), route)
        await service.handle(_message("ja"), route)

        assert harness.calls == []
        assert any("deaktiviert" in item.lower() for item in route.messages)

    _run(run())


def test_user_prompt_with_shell_syntax_is_never_split_into_commands(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path)
        route = Route()
        task = "prüfe dies; echo $(id); a && b | c > /tmp/x"

        await _propose_and_confirm(service, route, f"codex: mantis analysiere {task}")

        args, _kwargs = harness.calls[0]
        assert args.count(args[-1]) == 1
        assert task in args[-1]
        assert ";" in args[-1] and "$(id)" in args[-1] and "|" in args[-1]

    _run(run())


def test_confirmation_is_bound_to_the_unchanged_proposal(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path)
        route = Route()
        await service.handle(_message("codex: mantis analysiere erster-vorschlag"), route)
        first_id = next(iter(service.pending.values())).proposal_id

        await service.handle(_message("codex: mantis analysiere zweiter-vorschlag"), route)
        second_id = next(iter(service.pending.values())).proposal_id
        assert first_id != second_id

        await service.handle(_message("ja"), route)

        assert len(harness.calls) == 1
        assert "zweiter-vorschlag" in harness.calls[0][0][-1]
        assert "erster-vorschlag" not in harness.calls[0][0][-1]

    _run(run())


def test_rejected_or_unrelated_message_discards_open_proposal(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path)
        route = Route()
        await service.handle(_message("codex: mantis analysiere README"), route)

        await service.handle(_message("vielleicht später"), route)
        await service.handle(_message("ja"), route)

        assert harness.calls == []

    _run(run())


def test_confirmation_cannot_authorize_a_changed_proposal(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path)
        route = Route()
        await service.handle(_message("codex: mantis analysiere README"), route)
        proposal = service.pending["123"]
        service.pending["123"] = replace(proposal, task="schreibe eine andere Datei")

        await service.handle(_message("ja"), route)

        assert harness.calls == []
        assert "verändert" in route.messages[-1]

    _run(run())


def test_duplicate_confirmation_event_starts_exactly_one_process(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path)
        route = Route()
        await service.handle(_message("codex: mantis analysiere README"), route)
        confirmation = _message("ja")

        await service.handle(confirmation, route)
        await service.handle(confirmation, route)

        assert len(harness.calls) == 1
        await service.stop()

    _run(run())


def test_duplicate_confirmation_for_old_proposal_cannot_start_a_new_proposal(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path)
        route = Route()
        await service.handle(_message("codex: mantis analysiere erster"), route)
        old_confirmation = _message("ja")
        await service.handle(old_confirmation, route)
        await service.handle(_message("codex: mantis analysiere zweiter"), route)

        await service.handle(old_confirmation, route)

        assert len(harness.calls) == 1
        assert "zweiter" not in harness.calls[0][0][-1]
        assert "123" in service.pending

        await service.handle(_message("ja"), route)

        assert len(harness.calls) == 2
        assert "zweiter" in harness.calls[1][0][-1]
        await service.stop()

    _run(run())


def test_replayed_confirmation_after_restart_cannot_start_a_new_proposal(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path)
        route = Route()
        await service.handle(_message("codex: mantis analysiere erster"), route)
        old_confirmation = _message("ja")
        await service.handle(old_confirmation, route)
        await service.stop()

        restarted, _ = _service(git_repo, tmp_path, harness)
        await restarted.handle(_message("codex: mantis analysiere zweiter"), route)
        current = restarted.pending["123"]
        old_confirmation.raw.created_at = datetime.fromtimestamp(
            current.created_at - 1, tz=timezone.utc,
        )

        await restarted.handle(old_confirmation, route)

        assert len(harness.calls) == 1
        assert restarted.pending["123"].proposal_id == current.proposal_id

        await restarted.handle(_message("ja"), route)
        assert len(harness.calls) == 2
        assert "zweiter" in harness.calls[-1][0][-1]
        await restarted.stop()

    _run(run())


def test_symlinked_worktrees_directory_outside_repo_is_rejected(git_repo, tmp_path):
    async def run():
        outside = tmp_path / "outside"
        outside.mkdir()
        (git_repo / ".worktrees").symlink_to(outside, target_is_directory=True)
        service, harness = _service(git_repo, tmp_path)
        route = Route()

        await _propose_and_confirm(service, route, "codex: mantis schreibe eine Datei")

        assert harness.calls == []
        assert not list(outside.iterdir())
        assert "nicht gestartet" in route.messages[-1]

    _run(run())


def test_pid_reuse_or_identity_mismatch_never_receives_a_signal(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path)
        route = Route()
        await _propose_and_confirm(service, route, "codex: mantis analysiere PID")
        record = next(iter(service.runs.values()))
        service._process_identity = lambda _pid: "reused-pid-with-different-start-time"

        await service.handle(_message(f"abbrechen {record.remote_id}"), route)

        assert harness.signals == []
        assert record.status == "interrupted"
        await service.stop()

    _run(run())


def test_remote_state_files_are_private_and_do_not_store_the_prompt(git_repo, tmp_path):
    async def run():
        service, _harness = _service(git_repo, tmp_path)
        route = Route()
        await _propose_and_confirm(service, route, "codex: mantis analysiere private-prompt")
        record = next(iter(service.runs.values()))
        state_file = service._record_path(record.remote_id)
        stored = state_file.read_text()

        assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(state_file.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(service.state_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(service.runs_dir.stat().st_mode) == 0o700
        assert "private-prompt" not in stored
        await service.stop()

    _run(run())


def test_pending_confirmation_is_lost_on_mantis_restart(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path)
        route = Route()
        await service.handle(_message("codex: mantis analysiere README"), route)

        restarted, _ = _service(git_repo, tmp_path, harness)
        accepted = await restarted.handle(_message("ja"), route)

        assert restarted.pending == {}
        assert accepted is False
        assert harness.calls == []

    _run(run())


def test_running_process_is_recovered_without_restarting_or_signalling(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path)
        route = Route()
        await _propose_and_confirm(service, route, "codex: mantis analysiere Neustart")
        remote_id = next(iter(service.runs))
        await service.stop()

        restarted, _ = _service(git_repo, tmp_path, harness)
        await restarted.start()
        await asyncio.sleep(0)

        assert restarted.runs[remote_id].status == "running"
        assert len(harness.calls) == 1
        assert harness.signals == []
        await restarted.stop()

    _run(run())


@pytest.mark.parametrize(
    ("registry_status", "expected"),
    [("ok", "succeeded"), ("fail", "failed"), ("quota", "quota"), ("running", "interrupted")],
)
def test_restart_maps_registry_terminal_states_without_retry(
    git_repo, tmp_path, registry_status, expected,
):
    async def run():
        harness = ProcessHarness()
        service, _ = _service(git_repo, tmp_path, harness)
        route = Route()
        await _propose_and_confirm(service, route, "codex: mantis analysiere Neustartstatus")
        record = next(iter(service.runs.values()))
        folder = Path(record.tmp_dir) / "codex-task.FAKE123"
        folder.mkdir()
        (folder / "meta").write_text(
            f"pid={record.pid}\nstart=1\ndir={record.cwd}\nrun={folder}\ntitle=test\n",
        )
        (folder / "status").write_text(registry_status)
        (folder / "last.txt").write_text("nur angeforderte Antwort")
        record.registry_id = folder.name
        record.registry_run = str(folder)
        service._save_run(record)
        record.status = "running"
        service._save_run(record)
        await service.stop()

        dead = AgentRemoteService(
            service.config,
            popen=harness.popen,
            process_identity=lambda _pid: None,
            process_group=harness.process_group,
            signal_process_group=harness.signal_group,
        )
        await dead.start()

        assert dead.runs[record.remote_id].status == expected
        assert len(harness.calls) == 1
        assert harness.signals == []
        await dead.stop()

    _run(run())


def test_list_includes_manual_registry_ids_and_codex_ps_output(git_repo, tmp_path):
    async def run():
        home = tmp_path / "home"
        folder = home / ".local" / "state" / "codex-task" / "runs" / "codex-task.MANUAL"
        folder.mkdir(parents=True)
        (folder / "meta").write_text(
            f"pid=1\nstart=1\ndir={git_repo}\nrun=/tmp/codex-task.MANUAL\ntitle=manual task\n",
        )
        (folder / "status").write_text("ok")
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, "one local task", "")

        service, _ = _service(git_repo, tmp_path, command_runner=runner, home_dir=home)
        route = Route()
        await service.handle(_message("was läuft?"), route)

        assert "one local task" in route.messages[-1]
        assert "codex-task.MANUAL" in route.messages[-1]
        assert calls[0][0] == [str(Path("/fake/home/.local/bin/codex-ps").resolve())]

    _run(run())


def test_configured_pytest_vector_is_used_without_chat_arguments(git_repo, tmp_path):
    async def run():
        trusted_args = ("python3.14", "-m", "pytest", "tests/", "-q")
        service, harness = _service(
            git_repo, tmp_path, test_args={"mantis": trusted_args},
        )
        route = Route()
        await _propose_and_confirm(service, route, "codex: mantis schreibe eine Testdatei")
        record = next(iter(service.runs.values()))
        record.status = "succeeded"
        service._save_run(record)

        await service.handle(_message(f"tests in {record.remote_id}"), route)

        args, kwargs = harness.calls[-1]
        assert args == list(trusted_args)
        assert kwargs["cwd"] == record.worktree
        assert kwargs["shell"] is False
        assert len(harness.calls) == 2
        await service.stop()

    _run(run())


def test_fast_path_consumes_remote_message_before_normal_handler():
    async def run():
        from orchestrator import Orchestrator
        from unittest.mock import AsyncMock

        remote = SimpleNamespace(handle=AsyncMock(return_value=True))
        handler = SimpleNamespace(
            _channel_for=lambda _msg: "reply-route",
            handle=AsyncMock(),
        )
        orchestrator = object.__new__(Orchestrator)
        orchestrator.agent_remote = remote
        orchestrator.msg_handler = handler

        await orchestrator.handle_message(_message("was läuft?"))

        remote.handle.assert_awaited_once()
        handler.handle.assert_not_awaited()

    _run(run())


def test_agent_flow_persists_messages_with_agent_use_case(git_repo, tmp_path):
    async def run():
        persisted = []
        harness = ProcessHarness()
        config = AgentRemoteConfig(
            enabled=True,
            allowed_user_ids=("123",),
            repos={"mantis": str(git_repo)},
            state_dir=tmp_path / "state",
            home_dir=tmp_path / "home",
        )
        service = AgentRemoteService(
            config,
            persist=lambda role, content, **kwargs: persisted.append((role, content, kwargs)),
            popen=harness.popen,
            process_identity=harness.identity,
            process_group=harness.process_group,
            signal_process_group=harness.signal_group,
        )
        route = Route()

        await service.handle(_message("codex: mantis analysiere README"), route)
        await service.handle(_message("ja"), route)

        assert len(persisted) >= 4
        assert all(item[2]["channel"] == "discord" for item in persisted)
        assert all(item[2]["meta"] == {"use_case": "agent"} for item in persisted)
        await service.stop()

    _run(run())


def test_failed_completion_dm_is_retried_after_discord_becomes_available(git_repo, tmp_path):
    class FlakyRoute(Route):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def send(self, text):
            self.attempts += 1
            if self.attempts == 1:
                raise ConnectionError("discord not ready")
            self.messages.append(text)

    async def run():
        async def yield_now(_seconds):
            await asyncio.sleep(0)

        service, _harness = _service(git_repo, tmp_path, sleep=yield_now)
        route = Route()
        await _propose_and_confirm(service, route, "codex: mantis analysiere notification")
        record = next(iter(service.runs.values()))
        record.status = "succeeded"
        record.ended_at = 2000
        flaky = FlakyRoute()
        service._routes[record.remote_id] = flaky

        await service._notify_completion(record)
        for _ in range(5):
            await asyncio.sleep(0)

        assert flaky.attempts == 2
        assert record.notification_sent_at is not None
        assert record.remote_id in flaky.messages[0]
        await service.stop()

    _run(run())


def test_unclear_mode_requires_read_or_write_before_confirmation(git_repo, tmp_path):
    async def run():
        service, harness = _service(git_repo, tmp_path)
        route = Route()

        await service.handle(_message("codex: mantis this task needs attention"), route)
        assert harness.calls == []
        assert "lesen" in route.messages[-1] and "schreiben" in route.messages[-1]

        await service.handle(_message("schreiben"), route)
        assert harness.calls == []
        assert "schreiben" in route.messages[-1]
        await service.handle(_message("ja"), route)

        assert len(harness.calls) == 1
        assert harness.calls[0][1]["cwd"] != str(git_repo)
        await service.stop()

    _run(run())


def test_disabled_by_default_and_default_allowlist_is_only_mantis():
    config = AgentRemoteConfig()

    assert config.enabled is False
    assert dict(config.repos) == {"mantis": "~/Mantis"}
    assert dict(config.test_args) == {}


def test_discord_route_disables_mentions_for_untrusted_result_text():
    class AllowedMentions:
        @staticmethod
        def none():
            return "mentions-disabled"

    class Destination:
        def __init__(self):
            self.sent = []

        async def send(self, text, **kwargs):
            self.sent.append((text, kwargs))

    async def run():
        channel = DiscordChannel(
            enabled=False,
            discord_module=SimpleNamespace(AllowedMentions=AllowedMentions),
        )
        destination = Destination()
        await channel.for_message(SimpleNamespace(channel=destination)).send(
            "untrusted result: @everyone <@123>"
        )

        assert destination.sent == [
            ("untrusted result: @everyone <@123>", {"allowed_mentions": "mentions-disabled"}),
        ]

    _run(run())

