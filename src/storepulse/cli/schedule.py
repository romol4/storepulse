"""`storepulse schedule install/remove/show`: OS-native daily scheduling.

Jobs run ``sys.executable -m storepulse run``. Subprocess calls go through an injectable
runner (``Env.command_runner``), so tests never touch the real system.
"""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import os
import re
import subprocess
import sys
from pathlib import Path

from storepulse.cli.common import (
    APPLE_P8_SECRET,
    GOOGLE_SA_SECRET,
    CliError,
    CommandResult,
    CommandRunner,
    Env,
    confirm,
)
from storepulse.core import config
from storepulse.core.secrets import (
    PASSPHRASE_FILE_ENV,
    SECRETS_FILENAME,
    STORE_FILE,
    STORE_KEYRING,
    EncryptedFileStore,
    SecretStoreError,
)

SERVICE_NAME = "storepulse"
TASK_NAME = "Storepulse"
CRONTAB_MARKER = "# managed by storepulse schedule"
PASSPHRASE_FILENAME = "run-passphrase"  # noqa: S105 (filename, not a value)

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def default_runner(args: list[str]) -> CommandResult:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
    except FileNotFoundError:
        return CommandResult(127, "", f"{args[0]}: command not found")
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def _runner(env: Env) -> CommandRunner:
    return env.command_runner or default_runner


def validate_time(text: str) -> str:
    if not _TIME_RE.match(text):
        raise CliError(f"run time must be HH:MM (24-hour), got {text!r}")
    return text


# -- paths (Path.home() so tests can monkeypatch it) ----------------------------------------


def systemd_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def launchd_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def launchd_plist_path() -> Path:
    return launchd_dir() / "com.storepulse.run.plist"


def passphrase_file_path() -> Path:
    return config.config_dir() / PASSPHRASE_FILENAME


# -- content generators -----------------------------------------------------------------


def systemd_service_unit(passphrase_file: Path | None) -> str:
    # Quoted per systemd.service(5): an unquoted Environment= value containing a space
    # (e.g. a config dir under a "Documents and Settings"-style home) would otherwise
    # be split into multiple assignments.
    env_line = f'Environment="{PASSPHRASE_FILE_ENV}={passphrase_file}"\n' if passphrase_file else ""
    return (
        "[Unit]\n"
        "Description=Storepulse daily digest\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"{env_line}"
        f'ExecStart="{sys.executable}" -m storepulse run\n'
    )


def systemd_timer_unit(run_time: str) -> str:
    return (
        "[Unit]\n"
        "Description=Run Storepulse daily\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar=*-*-* {run_time}\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def launchd_plist(run_time: str, passphrase_file: Path | None) -> str:
    hour, minute = run_time.split(":")
    env_block = ""
    if passphrase_file is not None:
        env_block = (
            "\t<key>EnvironmentVariables</key>\n"
            "\t<dict>\n"
            f"\t\t<key>{PASSPHRASE_FILE_ENV}</key>\n"
            f"\t\t<string>{passphrase_file}</string>\n"
            "\t</dict>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "\t<key>Label</key>\n"
        "\t<string>com.storepulse.run</string>\n"
        "\t<key>ProgramArguments</key>\n"
        "\t<array>\n"
        f"\t\t<string>{sys.executable}</string>\n"
        "\t\t<string>-m</string>\n"
        "\t\t<string>storepulse</string>\n"
        "\t\t<string>run</string>\n"
        "\t</array>\n"
        "\t<key>StartCalendarInterval</key>\n"
        "\t<dict>\n"
        f"\t\t<key>Hour</key>\n\t\t<integer>{int(hour)}</integer>\n"
        f"\t\t<key>Minute</key>\n\t\t<integer>{int(minute)}</integer>\n"
        "\t</dict>\n"
        f"{env_block}"
        "</dict>\n"
        "</plist>\n"
    )


def schtasks_create_args(run_time: str) -> list[str]:
    tr_value = f'"{sys.executable}" -m storepulse run'
    return [
        "schtasks", "/Create", "/SC", "DAILY", "/ST", f"{run_time}:00",
        "/TN", TASK_NAME, "/TR", tr_value, "/F",
    ]  # fmt: skip


def schtasks_delete_args() -> list[str]:
    return ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]


def crontab_line(run_time: str, passphrase_file: Path | None) -> str:
    hour, minute = run_time.split(":")
    # Quoted so cron's shell doesn't split the assignment on a space in the path.
    env_prefix = f'{PASSPHRASE_FILE_ENV}="{passphrase_file}" ' if passphrase_file else ""
    return (
        f"{int(minute)} {int(hour)} * * * {env_prefix}"
        f'"{sys.executable}" -m storepulse run {CRONTAB_MARKER}'
    )


# -- passphrase file for unattended runs with the encrypted-file store -----------------------


def _write_passphrase_file(env: Env, cfg: config.Config) -> Path | None:
    env.say(
        "Your secrets are in a passphrase-protected file, and a scheduled run can't stop "
        "to ask for the passphrase. Storepulse can save it in its own 0600 file so the "
        "scheduled job can read it; anyone who can read that file plus your secrets file "
        "can unlock your credentials."
    )
    if not confirm(
        env, "Write a passphrase file so scheduled runs can unlock your secrets?", default=False
    ):
        return None
    passphrase = env.getpass("Secrets file passphrase: ")
    path = config.config_dir() / SECRETS_FILENAME
    probe_name = APPLE_P8_SECRET if cfg.apple is not None else GOOGLE_SA_SECRET
    try:
        # ignore_env=True: verify exactly the passphrase just typed, not one from a
        # stale STOREPULSE_PASSPHRASE(_FILE) left in this process's environment.
        if EncryptedFileStore(path, passphrase, ignore_env=True).get(probe_name) is None:
            raise CliError("that passphrase opened the file, but the saved key is missing")
    except SecretStoreError as exc:
        raise CliError(str(exc)) from None
    out = passphrase_file_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(passphrase)
    if os.name == "posix":
        os.chmod(out, 0o600)
    env.say(f"Passphrase saved to {out} (0600).")
    return out


def _remove_passphrase_file() -> None:
    path = passphrase_file_path()
    if path.exists():
        path.unlink()


# -- linux: systemd or crontab -----------------------------------------------------------


def _systemd_available(runner: CommandRunner) -> bool:
    return runner(["systemctl", "--user", "--version"]).ok


def linger_enabled(runner: CommandRunner, user: str) -> bool:
    result = runner(["loginctl", "show-user", user, "-p", "Linger"])
    return result.ok and "linger=yes" in result.stdout.lower()


def _current_user() -> str:
    try:
        return getpass.getuser()
    except OSError:
        return ""


def _install_systemd(
    env: Env, runner: CommandRunner, run_time: str, passphrase_file: Path | None
) -> None:
    unit_dir = systemd_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / f"{SERVICE_NAME}.service").write_text(systemd_service_unit(passphrase_file))
    (unit_dir / f"{SERVICE_NAME}.timer").write_text(systemd_timer_unit(run_time))
    reload_result = runner(["systemctl", "--user", "daemon-reload"])
    if not reload_result.ok:
        raise CliError(
            "systemctl daemon-reload failed: "
            f"{(reload_result.stderr or reload_result.stdout).strip()}"
        )
    enable_result = runner(["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.timer"])
    if not enable_result.ok:
        raise CliError(
            "systemctl enable --now failed: "
            f"{(enable_result.stderr or enable_result.stdout).strip()}"
        )
    env.say(f"systemd user timer installed: {unit_dir / f'{SERVICE_NAME}.timer'}")

    user = _current_user()
    if user and not linger_enabled(runner, user):
        env.say(
            "This session doesn't linger, so the timer only runs while you're logged in; "
            "a machine you access only over SSH may miss runs (README, 'Schedule "
            "management')."
        )
        if confirm(env, f"Run `loginctl enable-linger {user}` now?", default=True):
            result = runner(["loginctl", "enable-linger", user])
            if result.ok:
                env.say("Lingering enabled.")
            else:
                env.warn(f"could not enable lingering: {(result.stderr or result.stdout).strip()}")
        else:
            env.warn("lingering left off; the timer may not run while you're logged out")


def _remove_systemd(env: Env, runner: CommandRunner) -> None:
    runner(["systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.timer"])
    for suffix in ("service", "timer"):
        path = systemd_dir() / f"{SERVICE_NAME}.{suffix}"
        if path.exists():
            path.unlink()
    runner(["systemctl", "--user", "daemon-reload"])
    env.say("systemd user timer removed.")


def _install_crontab(
    env: Env,
    runner: CommandRunner,
    run_time: str,
    passphrase_file: Path | None,
    secret_store: str,
) -> None:
    if secret_store == STORE_KEYRING:
        env.warn(
            "no systemd user session available, falling back to crontab; cron jobs "
            "usually run without a desktop keychain session, so the OS-keychain secrets "
            "this was set up with may be unreachable when the job runs (README, "
            "'Schedule management')."
        )
    existing = runner(["crontab", "-l"])
    lines = [line for line in existing.stdout.splitlines() if CRONTAB_MARKER not in line]
    lines.append(crontab_line(run_time, passphrase_file))
    result = _write_crontab(runner, lines)
    if not result.ok:
        raise CliError(f"crontab install failed: {(result.stderr or result.stdout).strip()}")
    env.say("Installed a crontab entry (no systemd available).")


def _remove_crontab(env: Env, runner: CommandRunner) -> None:
    existing = runner(["crontab", "-l"])
    lines = [line for line in existing.stdout.splitlines() if CRONTAB_MARKER not in line]
    _write_crontab(runner, lines)
    env.say("Crontab entry removed.")


def _write_crontab(runner: CommandRunner, lines: list[str]) -> CommandResult:
    path = config.config_dir() / "crontab.tmp"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines)
    path.write_text(content + "\n" if content else "")
    try:
        return runner(["crontab", str(path)])
    finally:
        path.unlink(missing_ok=True)


def _crontab_installed(runner: CommandRunner) -> bool:
    result = runner(["crontab", "-l"])
    return any(CRONTAB_MARKER in line for line in result.stdout.splitlines())


# -- macOS: launchd ------------------------------------------------------------------------


def _uid() -> int:
    # Only ever called on macOS (guarded by sys.platform == "darwin" at the call site);
    # the explicit re-check here is what lets mypy --platform win32 skip this line.
    if sys.platform == "win32":
        raise AssertionError("launchd is never used on Windows")
    return os.getuid()


def _install_launchd(
    env: Env, runner: CommandRunner, run_time: str, passphrase_file: Path | None
) -> None:
    path = launchd_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(launchd_plist(run_time, passphrase_file))
    # bootout is expected to fail with nothing to unload on a first install; only
    # bootstrap (the call that actually loads the agent) is checked.
    runner(["launchctl", "bootout", f"gui/{_uid()}", str(path)])
    result = runner(["launchctl", "bootstrap", f"gui/{_uid()}", str(path)])
    if not result.ok:
        raise CliError(f"launchctl bootstrap failed: {(result.stderr or result.stdout).strip()}")
    env.say(f"launchd agent installed: {path}")


def _remove_launchd(env: Env, runner: CommandRunner) -> None:
    path = launchd_plist_path()
    runner(["launchctl", "bootout", f"gui/{_uid()}", str(path)])
    if path.exists():
        path.unlink()
    env.say("launchd agent removed.")


# -- windows: Task Scheduler ---------------------------------------------------------------


def _install_schtasks(env: Env, runner: CommandRunner, run_time: str) -> None:
    result = runner(schtasks_create_args(run_time))
    if not result.ok:
        raise CliError(f"schtasks failed: {(result.stderr or result.stdout).strip()}")
    env.say(f"Task Scheduler task {TASK_NAME!r} installed.")


def _remove_schtasks(env: Env, runner: CommandRunner) -> None:
    runner(schtasks_delete_args())
    env.say("Task Scheduler task removed.")


# -- commands --------------------------------------------------------------------------------


def _needs_passphrase_file(cfg: config.Config) -> bool:
    return cfg.secret_store == STORE_FILE


def _refuse_in_hosted_mode() -> None:
    if config.hosted_mode():
        raise CliError(
            "hosted mode has its own scheduler; set the daily run time in the web app's "
            "Settings page (README, 'Hosted mode')"
        )


def cmd_schedule_install(args: argparse.Namespace, env: Env) -> int:
    _refuse_in_hosted_mode()
    cfg = config.load()
    if cfg.email is None:
        raise CliError("set up email first; run `storepulse init`")
    run_time = validate_time(args.time or cfg.schedule.run_time)
    runner = _runner(env)

    if sys.platform == "win32" and _needs_passphrase_file(cfg):
        raise CliError(
            "scheduled runs on Windows need secrets in Windows Credential Manager; the "
            "encrypted-file store isn't supported for unattended Windows tasks yet. "
            "Re-run `storepulse init` somewhere Credential Manager is reachable, or run "
            "`storepulse run` by hand instead (README, 'Schedule management')."
        )

    passphrase_file = None
    if _needs_passphrase_file(cfg):
        passphrase_file = _write_passphrase_file(env, cfg)
        if passphrase_file is None:
            raise CliError(
                "schedule install needs the passphrase file for unattended runs; not installed"
            )

    if sys.platform == "darwin":
        _install_launchd(env, runner, run_time, passphrase_file)
    elif sys.platform == "win32":
        _install_schtasks(env, runner, run_time)
    elif _systemd_available(runner):
        _install_systemd(env, runner, run_time, passphrase_file)
    else:
        _install_crontab(env, runner, run_time, passphrase_file, cfg.secret_store)

    cfg.schedule = dataclasses.replace(cfg.schedule, run_time=run_time)
    config.save(cfg)
    env.say(f"Storepulse will run daily at {run_time}.")
    return 0


def cmd_schedule_remove(args: argparse.Namespace, env: Env) -> int:
    _refuse_in_hosted_mode()
    runner = _runner(env)
    if sys.platform == "darwin":
        _remove_launchd(env, runner)
    elif sys.platform == "win32":
        _remove_schtasks(env, runner)
    elif _systemd_available(runner):
        _remove_systemd(env, runner)
    else:
        _remove_crontab(env, runner)
    _remove_passphrase_file()
    return 0


def cmd_schedule_show(args: argparse.Namespace, env: Env) -> int:
    _refuse_in_hosted_mode()
    cfg = config.load()
    env.say(f"Configured run time: {cfg.schedule.run_time}")
    runner = _runner(env)
    if sys.platform == "darwin":
        path = launchd_plist_path()
        env.say(f"launchd: {'installed' if path.exists() else 'not installed'} ({path})")
    elif sys.platform == "win32":
        result = runner(["schtasks", "/Query", "/TN", TASK_NAME])
        env.say(f"Task Scheduler: {'installed' if result.ok else 'not installed'}")
    elif _systemd_available(runner):
        path = systemd_dir() / f"{SERVICE_NAME}.timer"
        env.say(f"systemd timer: {'installed' if path.exists() else 'not installed'} ({path})")
        user = _current_user()
        linger = bool(user) and linger_enabled(runner, user)
        env.say(f"Session lingering: {'on' if linger else 'off'}")
        if not linger:
            env.warn("without lingering, the timer only runs while you're logged in")
    else:
        installed = _crontab_installed(runner)
        env.say(f"crontab: {'installed' if installed else 'not installed'}")
    env.say(f"Passphrase file: {'present' if passphrase_file_path().exists() else 'not used'}")
    return 0
