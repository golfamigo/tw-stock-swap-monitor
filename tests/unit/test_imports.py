import json
import subprocess
import sys
import textwrap
from pathlib import Path

from fastapi import FastAPI


def test_application_factory_returns_fastapi_application() -> None:
    """The bootstrap package exposes an importable ASGI application factory."""
    import app.main

    application = app.main.create_application()

    assert isinstance(application, FastAPI)


def test_wheel_install_resolves_application_from_its_own_site_packages(tmp_path: Path) -> None:
    """A non-editable wheel install resolves app.main without the repository on sys.path."""
    repository_root = Path(__file__).resolve().parents[2]
    wheel_dir = tmp_path / "wheelhouse"
    wheel_dir.mkdir()

    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--no-index",
            "--wheel-dir",
            str(wheel_dir),
            str(repository_root),
        ],
        capture_output=True,
        cwd=tmp_path,
        text=True,
    )
    assert build.returncode == 0, (
        f"Wheel build failed with exit code {build.returncode}.\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )

    wheels = list(wheel_dir.glob("tw_stock_swap_monitor-*.whl"))
    assert len(wheels) == 1, f"Expected one project wheel, found: {wheels}"

    installed_venv = tmp_path / "installed-venv"
    create_venv = subprocess.run(
        [sys.executable, "-m", "venv", str(installed_venv)],
        capture_output=True,
        cwd=tmp_path,
        text=True,
    )
    assert create_venv.returncode == 0, (
        f"Temporary venv creation failed with exit code {create_venv.returncode}.\n"
        f"stdout:\n{create_venv.stdout}\nstderr:\n{create_venv.stderr}"
    )

    installed_python = installed_venv / "Scripts" / "python.exe"
    install = subprocess.run(
        [
            str(installed_python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            str(wheels[0]),
        ],
        capture_output=True,
        cwd=tmp_path,
        text=True,
    )
    assert install.returncode == 0, (
        f"Wheel installation failed with exit code {install.returncode}.\n"
        f"stdout:\n{install.stdout}\nstderr:\n{install.stderr}"
    )

    command = textwrap.dedent(
        """
        import importlib.util
        import json
        from pathlib import Path
        import sysconfig

        spec = importlib.util.find_spec("app.main")
        if spec is None or spec.origin is None:
            raise SystemExit("Installed wheel did not resolve app.main")

        module_path = Path(spec.origin).resolve()
        site_packages = Path(sysconfig.get_paths()["purelib"]).resolve()
        if not module_path.is_relative_to(site_packages):
            raise SystemExit(
                f"app.main resolved outside the temporary venv: {module_path}"
            )

        print(json.dumps({"module_path": str(module_path), "site_packages": str(site_packages)}))
        """
    )
    resolved = subprocess.run(
        [str(installed_python), "-I", "-c", command],
        capture_output=True,
        cwd=tmp_path,
        text=True,
    )
    assert resolved.returncode == 0, (
        f"Isolated wheel import resolution failed with exit code {resolved.returncode}.\n"
        f"stdout:\n{resolved.stdout}\nstderr:\n{resolved.stderr}"
    )
    payload = json.loads(resolved.stdout)

    assert Path(payload["module_path"]).is_relative_to(Path(payload["site_packages"]))
