import os
import shlex
import subprocess
from pathlib import Path


def run_postinstall(tmp_path, **flags):
    # Execute only the extracted scriptlet with fake Python/systemctl
    # binaries.
    # No pip, application initialization, RPM installation, or service
    # call runs.
    spec = (Path(__file__).parents[1] / "kubengine.spec").read_text()
    script = spec.split("\n%post\n", 1)[1].split("\n%postun\n", 1)[0]
    fake_python = tmp_path / "fake-python"
    fake_python.write_text("""#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_TRACE"
if [ "$2" = pip ]; then exit "${FAKE_PIP_EXIT:-0}"; fi
exit "${FAKE_INIT_EXIT:-0}"
""")
    fake_python.chmod(0o755)
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text("#!/bin/sh\nexit 0\n")
    fake_systemctl.chmod(0o755)
    script = script.replace("%{python311}", shlex.quote(str(fake_python)))
    script = script.replace("%{kubengine_dir}", str(tmp_path / "installation"))
    trace = tmp_path / "calls"
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "FAKE_TRACE": str(trace),
        **flags,
    }
    result = subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result, trace.read_text()


def test_dependency_failure_is_visible_and_stops_initialization(tmp_path):
    result, trace = run_postinstall(tmp_path, FAKE_PIP_EXIT="1")
    assert result.returncode != 0
    assert "dependency installation failed" in result.stderr
    assert "Installation completed" not in result.stdout
    assert "cli.app" not in trace


def test_initialization_uses_app_subcommand(tmp_path):
    result, trace = run_postinstall(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "-m cli.app app init-data" in trace


def test_initialization_failure_is_visible(tmp_path):
    result, trace = run_postinstall(tmp_path, FAKE_INIT_EXIT="1")
    assert result.returncode != 0
    assert "data initialization failed" in result.stderr
    assert "Installation completed" not in result.stdout


def test_existing_database_is_checked_at_actual_path(tmp_path):
    database = tmp_path / "installation/config/sqlite.db"
    database.parent.mkdir(parents=True)
    database.write_text("synthetic existing database")
    result, trace = run_postinstall(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "cli.app" not in trace
