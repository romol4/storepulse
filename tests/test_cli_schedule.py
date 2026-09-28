from __future__ import annotations

import io
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from storepulse.cli import schedule
from storepulse.cli.common import APPLE_P8_SECRET, CommandResult, Env
from storepulse.core import config
from storepulse.core.secrets import SECRETS_FILENAME, EncryptedFileStore


@dataclass
class FakeRunner:
    calls: list[list[str]] = field(default_factory=list)
    responses: dict[tuple[str, ...], CommandResult] = field(default_factory=dict)
    default: CommandResult = field(default_factory=lambda: CommandResult(0, "", ""))

    def __call__(self, args: list[str]) -> CommandResult:
        self.calls.append(args)
        return self.responses.get(tuple(args), self.default)


def _env(
    runner: FakeRunner, answers: list[str] | None = None, secrets: list[str] | None = None
) -> Env:
    it = iter(answers or [])
    passes = iter(secrets or ["x"] * 10)
    return Env(
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        input=lambda prompt: next(it),
        getpass=lambda prompt: next(passes),
        command_runner=runner,
    )


def _out(env: Env) -> str:
    return env.stdout.getvalue()  # type: ignore[attr-defined, no-any-return]


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


EMAIL_CFG = config.EmailConfig(
    host="h", port=1, security="ssl", username="u", from_addr="a@b.com", to_addrs=["c@d.com"]
)


def _cfg(**overrides: object) -> config.Config:
    defaults: dict[str, object] = {"email": EMAIL_CFG, "secret_store": "keyring"}
    defaults.update(overrides)
    return config.Config(**defaults)  # type: ignore[arg-type]


# -- content generators (pure functions) ------------------------------------------------


def test_systemd_service_unit_without_passphrase_file() -> None:
    unit = schedule.systemd_service_unit(None)
    assert "Type=oneshot" in unit
    assert "-m storepulse run" in unit
    assert "Environment=" not in unit


def test_systemd_service_unit_with_passphrase_file(tmp_path: Path) -> None:
    path = tmp_path / "run-passphrase"
    unit = schedule.systemd_service_unit(path)
    assert f"Environment=STOREPULSE_PASSPHRASE_FILE={path}" in unit


def test_systemd_timer_unit_format() -> None:
    unit = schedule.systemd_timer_unit("09:15")
    assert "OnCalendar=*-*-* 09:15" in unit
    assert "Persistent=true" in unit
    assert "WantedBy=timers.target" in unit


def test_launchd_plist_hour_minute() -> None:
    plist = schedule.launchd_plist("09:05", None)
    assert "<integer>9</integer>" in plist
    assert "<integer>5</integer>" in plist
    assert "EnvironmentVariables" not in plist


def test_launchd_plist_with_passphrase_file(tmp_path: Path) -> None:
    path = tmp_path / "run-passphrase"
    plist = schedule.launchd_plist("09:05", path)
    assert "EnvironmentVariables" in plist
    assert str(path) in plist


def test_schtasks_create_args_quotes_a_spaced_python_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # Windows Task Scheduler is picky about nested quoting; a python.exe under "Program
    # Files" is the common real-world case that breaks a naive /TR value.
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\Python311\python.exe")
    args = schedule.schtasks_create_args("10:30")
    tr_index = args.index("/TR")
    tr_value = args[tr_index + 1]
    assert tr_value == r'"C:\Program Files\Python311\python.exe" -m storepulse run'
    assert args[:2] == ["schtasks", "/Create"]
    assert "/F" in args
    assert "/TN" in args and schedule.TASK_NAME in args


def test_crontab_line_format() -> None:
    line = schedule.crontab_line("09:05", None)
    assert line.startswith("5 9 * * * ")
    assert line.endswith(schedule.CRONTAB_MARKER)
    assert "STOREPULSE_PASSPHRASE_FILE" not in line


def test_crontab_line_with_passphrase_file(tmp_path: Path) -> None:
    path = tmp_path / "run-passphrase"
    line = schedule.crontab_line("09:05", path)
    assert f"STOREPULSE_PASSPHRASE_FILE={path} " in line


# -- install: per-OS dispatch -------------------------------------------------------------


def test_install_requires_email_first() -> None:
    config.save(config.Config())
    from storepulse.cli.common import CliError

    with pytest.raises(CliError, match="set up email"):
        schedule.cmd_schedule_install(_ns(), _env(FakeRunner()))


def _ns(time: str | None = None) -> object:
    class Namespace:
        pass

    ns = Namespace()
    ns.time = time  # type: ignore[attr-defined]
    return ns


def test_install_darwin_writes_plist_and_bootstraps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(schedule, "_uid", lambda: 501)
    config.save(_cfg())
    runner = FakeRunner()
    env = _env(runner)
    assert schedule.cmd_schedule_install(_ns("08:00"), env) == 0
    assert schedule.launchd_plist_path().exists()
    bootstrap_calls = [c for c in runner.calls if c[:2] == ["launchctl", "bootstrap"]]
    assert bootstrap_calls == [
        ["launchctl", "bootstrap", "gui/501", str(schedule.launchd_plist_path())]
    ]
    assert config.load().schedule.run_time == "08:00"


def test_install_windows_calls_schtasks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    config.save(_cfg())
    runner = FakeRunner()
    assert schedule.cmd_schedule_install(_ns("08:00"), _env(runner)) == 0
    assert any(c[:2] == ["schtasks", "/Create"] for c in runner.calls)


def test_install_windows_schtasks_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    config.save(_cfg())
    runner = FakeRunner(default=CommandResult(1, "", "access denied"))
    from storepulse.cli.common import CliError

    with pytest.raises(CliError, match="access denied"):
        schedule.cmd_schedule_install(_ns("08:00"), _env(runner))


def test_install_windows_with_file_store_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    config.save(_cfg(secret_store="file"))
    from storepulse.cli.common import CliError

    with pytest.raises(CliError, match="Credential Manager"):
        schedule.cmd_schedule_install(_ns("08:00"), _env(FakeRunner()))


def test_install_linux_uses_systemd_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    config.save(_cfg())
    runner = FakeRunner(
        responses={("systemctl", "--user", "--version"): CommandResult(0, "255", "")}
    )
    assert schedule.cmd_schedule_install(_ns("08:00"), _env(runner, ["n"])) == 0
    assert (schedule.systemd_dir() / "storepulse.timer").exists()
    assert not any(c[0] == "crontab" for c in runner.calls)


def test_install_linux_falls_back_to_crontab_without_systemd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    config.save(_cfg())
    runner = FakeRunner(
        responses={("systemctl", "--user", "--version"): CommandResult(1, "", "not found")}
    )
    assert schedule.cmd_schedule_install(_ns("08:00"), _env(runner)) == 0
    crontab_write_calls = [c for c in runner.calls if c[0] == "crontab" and c[1] != "-l"]
    assert len(crontab_write_calls) == 1
    assert not (schedule.systemd_dir() / "storepulse.timer").exists()


def test_install_default_time_comes_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    config.save(_cfg(schedule=config.ScheduleConfig(run_time="14:45")))
    runner = FakeRunner(
        responses={("systemctl", "--user", "--version"): CommandResult(0, "255", "")}
    )
    assert schedule.cmd_schedule_install(_ns(None), _env(runner, ["n"])) == 0
    assert "OnCalendar=*-*-* 14:45" in (schedule.systemd_dir() / "storepulse.timer").read_text()


def test_install_rejects_bad_time_format() -> None:
    config.save(_cfg())
    from storepulse.cli.common import CliError

    with pytest.raises(CliError, match="HH:MM"):
        schedule.cmd_schedule_install(_ns("9am"), _env(FakeRunner()))


# -- linger ------------------------------------------------------------------------------


def test_linger_enabled_true() -> None:
    runner = FakeRunner(
        responses={
            ("loginctl", "show-user", "bob", "-p", "Linger"): CommandResult(0, "Linger=yes\n", "")
        }
    )
    assert schedule.linger_enabled(runner, "bob") is True


def test_linger_enabled_false() -> None:
    runner = FakeRunner(
        responses={
            ("loginctl", "show-user", "bob", "-p", "Linger"): CommandResult(0, "Linger=no\n", "")
        }
    )
    assert schedule.linger_enabled(runner, "bob") is False


def test_install_systemd_offers_enable_linger_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(schedule, "_current_user", lambda: "bob")
    config.save(_cfg())
    runner = FakeRunner(
        responses={
            ("systemctl", "--user", "--version"): CommandResult(0, "255", ""),
            ("loginctl", "show-user", "bob", "-p", "Linger"): CommandResult(0, "Linger=no", ""),
        }
    )
    env = _env(runner, ["y"])
    assert schedule.cmd_schedule_install(_ns("08:00"), env) == 0
    assert ["loginctl", "enable-linger", "bob"] in runner.calls
    assert "Lingering enabled." in _out(env)


def test_install_systemd_skips_prompt_when_linger_already_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(schedule, "_current_user", lambda: "bob")
    config.save(_cfg())
    runner = FakeRunner(
        responses={
            ("systemctl", "--user", "--version"): CommandResult(0, "255", ""),
            ("loginctl", "show-user", "bob", "-p", "Linger"): CommandResult(0, "Linger=yes", ""),
        }
    )
    env = _env(runner)  # no answers needed: no prompt should be asked
    assert schedule.cmd_schedule_install(_ns("08:00"), env) == 0
    assert not any(c[:2] == ["loginctl", "enable-linger"] for c in runner.calls)


def test_show_reports_linger_off_with_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(schedule, "_current_user", lambda: "bob")
    config.save(_cfg())
    runner = FakeRunner(
        responses={
            ("systemctl", "--user", "--version"): CommandResult(0, "255", ""),
            ("loginctl", "show-user", "bob", "-p", "Linger"): CommandResult(0, "Linger=no", ""),
        }
    )
    env = _env(runner)
    assert schedule.cmd_schedule_show(_ns(), env) == 0
    combined = _out(env) + env.stderr.getvalue()  # type: ignore[attr-defined]
    assert "Session lingering: off" in combined
    assert "warning" in combined.lower()


# -- passphrase file for the encrypted-file store -----------------------------------------


def _file_store_cfg(tmp_path: Path, passphrase: str) -> config.Config:
    store_path = config.config_dir() / SECRETS_FILENAME
    EncryptedFileStore(store_path, passphrase).set(APPLE_P8_SECRET, "fake-p8-contents")
    return _cfg(apple=config.AppleConfig("i", "k", "1"), secret_store="file")


def test_write_passphrase_file_creates_0600_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    cfg = _file_store_cfg(Path("unused"), "correct horse")
    config.save(cfg)
    runner = FakeRunner(
        responses={("systemctl", "--user", "--version"): CommandResult(0, "255", "")}
    )
    env = _env(runner, ["y", "n"], secrets=["correct horse"])  # confirm file, decline linger
    assert schedule.cmd_schedule_install(_ns("08:00"), env) == 0
    path = schedule.passphrase_file_path()
    assert path.exists()
    assert path.read_text() == "correct horse"
    if hasattr(__import__("os"), "getuid"):
        import stat

        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (
        f"STOREPULSE_PASSPHRASE_FILE={path}"
        in (schedule.systemd_dir() / "storepulse.service").read_text()
    )


def test_write_passphrase_file_declined_aborts_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    cfg = _file_store_cfg(Path("unused"), "correct horse")
    config.save(cfg)
    from storepulse.cli.common import CliError

    env = _env(FakeRunner(), ["n"])
    with pytest.raises(CliError, match="not installed"):
        schedule.cmd_schedule_install(_ns("08:00"), env)
    assert not schedule.passphrase_file_path().exists()


def test_write_passphrase_file_wrong_passphrase_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    cfg = _file_store_cfg(Path("unused"), "correct horse")
    config.save(cfg)
    from storepulse.cli.common import CliError

    env = _env(FakeRunner(), ["y"], secrets=["wrong passphrase"])
    with pytest.raises(CliError):
        schedule.cmd_schedule_install(_ns("08:00"), env)
    assert not schedule.passphrase_file_path().exists()


def test_remove_deletes_passphrase_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    path = schedule.passphrase_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("secret")
    runner = FakeRunner(
        responses={("systemctl", "--user", "--version"): CommandResult(0, "255", "")}
    )
    assert schedule.cmd_schedule_remove(_ns(), _env(runner)) == 0
    assert not path.exists()


# -- crontab: marked line, doesn't disturb the rest of the user's crontab -----------------


def test_install_crontab_preserves_other_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    config.save(_cfg())
    existing = "0 3 * * * /usr/bin/some-other-job\n"
    written: dict[str, str] = {}

    def runner(args: list[str]) -> CommandResult:
        if args == ["systemctl", "--user", "--version"]:
            return CommandResult(1, "", "not found")
        if args == ["crontab", "-l"]:
            return CommandResult(0, existing, "")
        if args[0] == "crontab":
            written["content"] = Path(args[1]).read_text()
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")

    assert schedule.cmd_schedule_install(_ns("08:00"), _env(runner)) == 0
    assert "some-other-job" in written["content"]
    assert schedule.CRONTAB_MARKER in written["content"]


def test_remove_crontab_only_removes_the_marked_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    existing = f"0 3 * * * /usr/bin/keep-me\n0 8 * * * /old {schedule.CRONTAB_MARKER}\n"
    written: dict[str, str] = {}

    def runner(args: list[str]) -> CommandResult:
        if args == ["systemctl", "--user", "--version"]:
            return CommandResult(1, "", "not found")
        if args == ["crontab", "-l"]:
            return CommandResult(0, existing, "")
        if args[0] == "crontab":
            written["content"] = Path(args[1]).read_text()
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")

    assert schedule.cmd_schedule_remove(_ns(), _env(runner)) == 0
    assert "keep-me" in written["content"]
    assert schedule.CRONTAB_MARKER not in written["content"]


def test_crontab_installed_detection() -> None:
    runner = FakeRunner(
        responses={
            ("crontab", "-l"): CommandResult(0, f"* * * * * x {schedule.CRONTAB_MARKER}\n", "")
        }
    )
    assert schedule._crontab_installed(runner) is True
    runner2 = FakeRunner(responses={("crontab", "-l"): CommandResult(0, "", "")})
    assert schedule._crontab_installed(runner2) is False
