"""`claude-tts daemon install-service`: the LaunchAgent written and started from the package.

Every command (launchctl, plutil) and every probe (pid liveness, pid identity) is injected through
daemon_service.Env, and the daemon's files are redirected into tmp_path. An autouse fixture makes
any unpatched subprocess.run fail the test, so nothing here runs on the real machine.
"""

from __future__ import annotations

import plistlib
import subprocess
import time
from pathlib import Path

import pytest

from claude_code_tts import __version__, cli, state
from claude_code_tts import daemon_service as ds
from claude_code_tts import install as inst

REPO_TEMPLATE = Path(__file__).resolve().parents[1] / "services" / ds.PLIST_NAME
BIN = "/opt/test-tools/bin/claude-tts"
UID = 501
DAEMON_PID = 4242


@pytest.fixture(autouse=True)
def no_real_subprocess(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"a test reached a real subprocess: {args[0] if args else kwargs}")

    monkeypatch.setattr(subprocess, "run", refuse)


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """A throwaway state directory and home, with the daemon's files redirected into the state directory."""
    state_dir = tmp_path / "claude-tts"
    state_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(state, "PID_FILE", state_dir / "daemon.pid")
    monkeypatch.setattr(state, "LOCK_FILE", state_dir / "daemon.lock")
    monkeypatch.setattr(state, "HEARTBEAT_FILE", state_dir / "daemon.heartbeat")
    monkeypatch.setattr(state, "VERSION_FILE", state_dir / "daemon.version")
    return state_dir, home


def _tree(root: Path) -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        out[rel] = path.read_bytes() if path.is_file() else None
    return out


class Fake:
    """Answers launchctl and plutil, and records every command in order."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.print_rc = 113  # not loaded
        self.bootout_rc = 0
        self.bootstrap_rc = 0
        self.bootstrap_text = ""
        self.plutil_rc = 0
        self.plutil_text = ""

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        if argv[:2] == ["launchctl", "print"]:
            return self.print_rc, "", ""
        if argv[:2] == ["launchctl", "bootout"]:
            return self.bootout_rc, "", ""
        if argv[:2] == ["launchctl", "bootstrap"]:
            return self.bootstrap_rc, "", self.bootstrap_text
        if argv[0] == "plutil":
            return self.plutil_rc, "", self.plutil_text
        raise AssertionError(f"an unexpected command ran: {argv}")

    def launchctl(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "launchctl"]


class Clock:
    """A clock that moves only when the command sleeps. When daemon_at is reached, the daemon writes its files."""

    def __init__(self, daemon_at: float | None = None) -> None:
        self.now = 0.0
        self.sleeps = 0
        self.daemon_at = daemon_at

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1
        if self.daemon_at is not None and self.now >= self.daemon_at:
            self.start_daemon()

    def start_daemon(self) -> None:
        state.PID_FILE.write_text(str(DAEMON_PID))
        state.HEARTBEAT_FILE.write_text(str(time.time()))
        state.VERSION_FILE.with_name("daemon.release").write_text(__version__)


def make_env(home: Path, fake: Fake, clock: Clock | None = None, **over) -> ds.Env:
    clock = clock or Clock()
    fields: dict[str, object] = {
        "platform": "macos",
        "home": home,
        "uid": UID,
        "claude_tts_bin": BIN,
        "template": REPO_TEMPLATE,
        "run": fake,
        "alive": lambda pid: pid == DAEMON_PID,
        "is_daemon": lambda pid: None,
        "clock": clock,
        "sleep": clock.sleep,
        "wait_s": 20.0,
        "poll_s": 0.5,
    }
    fields.update(over)
    return ds.Env(**fields)


def rendered_for(state_dir: Path, home: Path) -> str:
    return ds.render_plist(REPO_TEMPLATE.read_text(), claude_tts_bin=BIN, state_dir=state_dir, home=home)


def plist_at(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / ds.PLIST_NAME


class TestRender:
    def test_program_arguments_and_paths_for_this_user(self, dirs):
        state_dir, home = dirs
        text = rendered_for(state_dir, home)
        assert ds.plist_problem(text, claude_tts_bin=BIN, state_dir=state_dir) is None
        data = plistlib.loads(text.encode("utf-8"))
        assert data["Label"] == ds.LABEL
        assert data["ProgramArguments"] == [BIN, "daemon", "foreground"]
        assert data["WorkingDirectory"] == str(home)
        assert data["StandardOutPath"] == str(state_dir / "daemon.stdout.log")
        assert data["StandardErrorPath"] == str(state_dir / "daemon.stderr.log")
        assert data["EnvironmentVariables"]["PATH"].split(":")[-1] == "/opt/test-tools/bin"

    def test_a_path_with_xml_characters_round_trips(self, dirs):
        state_dir, home = dirs
        odd_bin = "/opt/a&b/<x>/claude-tts"
        text = ds.render_plist(REPO_TEMPLATE.read_text(), claude_tts_bin=odd_bin, state_dir=state_dir, home=home)
        assert ds.plist_problem(text, claude_tts_bin=odd_bin, state_dir=state_dir) is None
        assert plistlib.loads(text.encode("utf-8"))["ProgramArguments"][0] == odd_bin

    def test_a_leftover_placeholder_is_refused(self, dirs):
        state_dir, _home = dirs
        problem = ds.plist_problem("<plist>CLAUDE_TTS_BIN</plist>", claude_tts_bin=BIN, state_dir=state_dir)
        assert problem is not None and "placeholder" in problem

    def test_a_program_other_than_this_binary_is_refused(self, dirs):
        state_dir, home = dirs
        text = rendered_for(state_dir, home)
        problem = ds.plist_problem(text, claude_tts_bin="/opt/other/claude-tts", state_dir=state_dir)
        assert problem is not None and "ProgramArguments" in problem


class TestDryRun:
    def test_writes_and_runs_nothing(self, dirs, capsys):
        state_dir, home = dirs
        before = (_tree(state_dir), _tree(home))
        fake = Fake()
        rc = ds.install_launchagent(dry_run=True, env=make_env(home, fake))
        out = capsys.readouterr().out
        assert rc == 0
        assert fake.calls == []
        assert (_tree(state_dir), _tree(home)) == before
        assert not (home / "Library").exists()
        for expected in (
            str(plist_at(home)),
            f"launchctl print gui/{UID}/{ds.LABEL}",
            f"launchctl bootstrap gui/{UID} {plist_at(home)}",
            "plutil -lint",
            "<key>ProgramArguments</key>",
            "a real run would proceed",
        ):
            assert expected in out, expected

    def test_reports_the_refusal_it_would_hit_and_exits_1(self, dirs, capsys):
        state_dir, home = dirs
        state.PID_FILE.write_text(str(DAEMON_PID))
        fake = Fake()
        rc = ds.install_launchagent(dry_run=True, env=make_env(home, fake, is_daemon=lambda pid: True))
        out = capsys.readouterr().out
        assert rc == 1
        assert fake.calls == []
        assert "a real run would refuse" in out and "already running" in out
        assert not plist_at(home).exists()


class TestRefusals:
    def test_a_daemon_alive_on_this_mac_refuses_in_one_line(self, dirs, capsys):
        state_dir, home = dirs
        state.PID_FILE.write_text(str(DAEMON_PID))
        fake = Fake()
        rc = ds.install_launchagent(env=make_env(home, fake, is_daemon=lambda pid: True))
        err = capsys.readouterr().err.strip().splitlines()
        assert rc == 1
        assert len(err) == 1 and "already running" in err[0] and "claude-tts daemon stop" in err[0]
        assert fake.calls == []
        assert not plist_at(home).exists()

    def test_refuses_a_fresh_heartbeat_with_no_local_daemon(self, dirs, capsys):
        """The shared-directory case: another machine writes this directory, so two daemons would play everything twice.

        The pid file names a live process that is not a daemon (a pid reused on this Mac), so no daemon runs here.
        """
        state_dir, home = dirs
        state.PID_FILE.write_text(str(DAEMON_PID))
        state.HEARTBEAT_FILE.write_text(str(time.time()))
        fake = Fake()
        rc = ds.install_launchagent(env=make_env(home, fake, is_daemon=lambda pid: False))
        err = capsys.readouterr().err
        assert rc == 1
        assert "another machine shares it" in err and "two daemons would play every message twice" in err
        assert fake.calls == []
        assert not plist_at(home).exists()

    def test_the_shared_directory_refusal_is_not_overridden_by_force(self, dirs, capsys):
        state_dir, home = dirs
        state.HEARTBEAT_FILE.write_text(str(time.time()))
        fake = Fake()
        rc = ds.install_launchagent(force=True, env=make_env(home, fake))
        assert rc == 1
        assert "another machine shares it" in capsys.readouterr().err
        assert fake.calls == []

    def test_a_heartbeat_older_than_30_seconds_is_not_a_daemon(self, dirs):
        state_dir, home = dirs
        state.HEARTBEAT_FILE.write_text(str(time.time() - 120))
        fake = Fake()
        assert ds.install_launchagent(env=make_env(home, fake, clock=Clock(daemon_at=0.5))) == 0

    def test_a_different_plist_refuses_without_force_and_is_left_alone(self, dirs, capsys):
        state_dir, home = dirs
        plist_at(home).parent.mkdir(parents=True)
        plist_at(home).write_bytes(b"<plist>hand written</plist>")
        fake = Fake()
        rc = ds.install_launchagent(env=make_env(home, fake))
        err = capsys.readouterr().err
        assert rc == 1
        assert "--force" in err and "different content" in err
        assert plist_at(home).read_bytes() == b"<plist>hand written</plist>"
        assert fake.calls == []


class TestForce:
    def test_force_replaces_a_different_plist_and_reloads_it_in_order(self, dirs):
        state_dir, home = dirs
        plist_at(home).parent.mkdir(parents=True)
        plist_at(home).write_bytes(b"<plist>hand written</plist>")
        fake = Fake()
        fake.print_rc = 0  # loaded from the old plist
        rc = ds.install_launchagent(force=True, env=make_env(home, fake, clock=Clock(daemon_at=0.5)))
        assert rc == 0
        assert plist_at(home).read_bytes() == rendered_for(state_dir, home).encode("utf-8")
        assert fake.launchctl() == ["print", "bootout", "bootstrap"]


class TestLoad:
    def test_fresh_install_bootstraps_and_waits_for_pid_and_heartbeat(self, dirs, capsys):
        state_dir, home = dirs
        fake = Fake()
        clock = Clock(daemon_at=2.0)
        rc = ds.install_launchagent(env=make_env(home, fake, clock=clock))
        out = capsys.readouterr().out
        assert rc == 0
        assert clock.sleeps >= 1, "success must wait for the daemon, not check once"
        assert fake.launchctl() == ["print", "bootstrap"]
        assert fake.calls[-1] == ["launchctl", "bootstrap", f"gui/{UID}", str(plist_at(home))]
        assert f"daemon.pid: {DAEMON_PID}" in out
        assert f"daemon.release: {__version__}" in out
        assert "heartbeat:" in out
        assert plist_at(home).read_bytes() == rendered_for(state_dir, home).encode("utf-8")

    def test_identical_plist_already_loaded_is_not_bootstrapped_again(self, dirs):
        state_dir, home = dirs
        plist_at(home).parent.mkdir(parents=True)
        plist_at(home).write_bytes(rendered_for(state_dir, home).encode("utf-8"))
        fake = Fake()
        fake.print_rc = 0
        assert ds.install_launchagent(env=make_env(home, fake, clock=Clock(daemon_at=0.5))) == 0
        assert fake.launchctl() == ["print"]

    def test_identical_plist_not_loaded_is_bootstrapped(self, dirs):
        state_dir, home = dirs
        plist_at(home).parent.mkdir(parents=True)
        plist_at(home).write_bytes(rendered_for(state_dir, home).encode("utf-8"))
        fake = Fake()
        assert ds.install_launchagent(env=make_env(home, fake, clock=Clock(daemon_at=0.5))) == 0
        assert fake.launchctl() == ["print", "bootstrap"]

    def test_domain_unavailable_says_run_it_at_the_console(self, dirs, capsys):
        state_dir, home = dirs
        fake = Fake()
        fake.bootstrap_rc = 125
        fake.bootstrap_text = "Bootstrap failed: 125: Domain does not support specified action"
        clock = Clock(daemon_at=0.5)
        rc = ds.install_launchagent(env=make_env(home, fake, clock=clock))
        err = capsys.readouterr().err
        assert rc == 1
        assert "Mac's own console" in err and "claude-tts daemon install-service" in err
        assert clock.sleeps == 0, "no wait when the service never loaded"

    def test_other_bootstrap_failure_is_reported_with_launchctl_text(self, dirs, capsys):
        state_dir, home = dirs
        fake = Fake()
        fake.bootstrap_rc = 5
        fake.bootstrap_text = "Bootstrap failed: 5: Input/output error"
        rc = ds.install_launchagent(env=make_env(home, fake))
        err = capsys.readouterr().err
        assert rc == 1
        assert "Input/output error" in err and "console" not in err

    def test_plutil_rejection_keeps_the_old_plist_and_leaves_no_temp_file(self, dirs, capsys):
        state_dir, home = dirs
        plist_at(home).parent.mkdir(parents=True)
        plist_at(home).write_bytes(b"<plist>old</plist>")
        fake = Fake()
        fake.plutil_rc = 1
        fake.plutil_text = "Property List error"
        rc = ds.install_launchagent(force=True, env=make_env(home, fake))
        assert rc == 1
        assert plist_at(home).read_bytes() == b"<plist>old</plist>"
        assert sorted(p.name for p in plist_at(home).parent.iterdir()) == [ds.PLIST_NAME]
        assert fake.launchctl() == []
        assert "plutil rejected" in capsys.readouterr().err

    def test_a_daemon_that_never_comes_up_exits_1_after_the_wait(self, dirs, capsys):
        state_dir, home = dirs
        fake = Fake()
        clock = Clock(daemon_at=None)
        rc = ds.install_launchagent(env=make_env(home, fake, clock=clock))
        err = capsys.readouterr().err
        assert rc == 1
        assert clock.now >= 20.0
        assert "no daemon heartbeat came within 20s" in err


class TestPlatformAndTemplate:
    def test_linux_is_macos_only_and_exits_2(self, dirs, capsys):
        state_dir, home = dirs
        fake = Fake()
        rc = ds.install_launchagent(env=make_env(home, fake, platform="linux"))
        assert rc == 2
        assert "macOS-only" in capsys.readouterr().err
        assert fake.calls == []
        assert not (home / "Library").exists()

    def test_missing_template_is_refused(self, dirs, capsys):
        state_dir, home = dirs
        rc = ds.install_launchagent(env=make_env(home, Fake(), template=None))
        assert rc == 1
        assert ds.PLIST_NAME in capsys.readouterr().err

    def test_template_is_found_in_the_package_when_repo_has_no_services(self, tmp_path, monkeypatch):
        pkg = tmp_path / "pkg"
        (pkg / "services").mkdir(parents=True)
        (pkg / "services" / ds.PLIST_NAME).write_text("<plist/>")
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(ds, "PACKAGE_DIR", pkg)
        assert ds.service_template(ds.PLIST_NAME, repo) == pkg / "services" / ds.PLIST_NAME
        assert ds.service_template(ds.PLIST_NAME, None) == pkg / "services" / ds.PLIST_NAME

    def test_the_checkout_template_is_preferred_over_the_package(self, tmp_path, monkeypatch):
        pkg = tmp_path / "pkg"
        (pkg / "services").mkdir(parents=True)
        (pkg / "services" / ds.PLIST_NAME).write_text("<plist/>")
        repo = tmp_path / "repo"
        (repo / "services").mkdir(parents=True)
        (repo / "services" / ds.PLIST_NAME).write_text("<plist/>")
        monkeypatch.setattr(ds, "PACKAGE_DIR", pkg)
        assert ds.service_template(ds.PLIST_NAME, repo) == repo / "services" / ds.PLIST_NAME

    def test_no_template_anywhere_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ds, "PACKAGE_DIR", tmp_path / "empty-pkg")
        assert ds.service_template(ds.PLIST_NAME, tmp_path / "empty-repo") is None

    def test_installer_and_command_share_one_lookup(self):
        assert inst.service_template is ds.service_template


class TestUpgradeNeverReachesInstallService:
    @pytest.fixture
    def wheel_home(self, tmp_path, monkeypatch):
        """A home, a ~/.claude-tts and a package holding the template, all under tmp; any subprocess fails the test.

        The installer's service step runs for real here (not dry): nothing it touches is outside tmp_path.
        """
        pkg = tmp_path / "pkg"
        (pkg / "services").mkdir(parents=True)
        (pkg / "services" / ds.PLIST_NAME).write_text(REPO_TEMPLATE.read_text())
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(ds, "PACKAGE_DIR", pkg)
        monkeypatch.setattr(inst, "REPO_DIR", pkg)
        monkeypatch.setattr(inst, "HOME", home)
        monkeypatch.setattr(inst, "TTS_CONFIG_DIR", home / ".claude-tts")
        monkeypatch.setattr(inst, "PLATFORM", "macos")

        def no_subprocess(*args, **kwargs):
            raise AssertionError(f"the service step reached a real subprocess: {args[0] if args else kwargs}")

        monkeypatch.setattr(inst.subprocess, "run", no_subprocess)
        return home

    def test_an_upgrade_from_a_wheel_stages_the_template_and_loads_nothing(self, wheel_home, capsys):
        inst.install_service_files(dry_run=False, upgrade=True)
        out = capsys.readouterr().out
        assert (wheel_home / ".claude-tts" / "services" / ds.PLIST_NAME).is_file(), "staged"
        assert not (wheel_home / "Library" / "LaunchAgents").exists(), "a packaged template is never deployed"
        assert "run: claude-tts daemon install-service" not in out, "an upgrade stays silent"

    def test_a_fresh_install_from_a_wheel_names_install_service_and_loads_nothing(self, wheel_home, capsys):
        inst.install_service_files(dry_run=False, upgrade=False)
        out = capsys.readouterr().out
        assert not (wheel_home / "Library" / "LaunchAgents").exists()
        assert "run: claude-tts daemon install-service" in out

    def test_do_install_runs_the_service_step_with_its_own_upgrade_flag(self):
        """do_install hands its upgrade flag to the step the tests above drive."""
        source = Path(inst.__file__).read_text()
        assert "install_service_files(dry_run=dry_run, upgrade=upgrade)" in source

    def test_the_installer_never_calls_the_command_function(self):
        source = Path(inst.__file__).read_text()
        assert "install_launchagent" not in source


class TestCli:
    def test_install_service_flags_reach_the_command(self, monkeypatch):
        seen: dict[str, object] = {}

        def record(**kwargs):
            seen.update(kwargs)
            return 0

        monkeypatch.setattr(ds, "install_launchagent", record)
        with pytest.raises(SystemExit) as exit_info:
            cli.main(["daemon", "install-service", "--dry-run", "--force"])
        assert exit_info.value.code == 0
        assert seen == {"dry_run": True, "force": True}

    def test_the_commands_exit_code_is_the_process_exit_code(self, monkeypatch):
        monkeypatch.setattr(ds, "install_launchagent", lambda **kwargs: 2)
        with pytest.raises(SystemExit) as exit_info:
            cli.main(["daemon", "install-service"])
        assert exit_info.value.code == 2
