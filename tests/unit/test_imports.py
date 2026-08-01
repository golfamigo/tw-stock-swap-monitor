import json
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI


def test_application_factory_returns_fastapi_application() -> None:
    """The bootstrap package exposes an importable ASGI application factory."""
    import app.main

    application = app.main.create_application()

    assert isinstance(application, FastAPI)


def test_installed_distribution_imports_this_worktrees_application(tmp_path: Path) -> None:
    """The installed distribution, not a globally installed app package, provides app.main."""
    command = """
import json
from importlib.metadata import version
from pathlib import Path

import app.main

print(json.dumps({
    \"distribution_version\": version(\"tw-stock-swap-monitor\"),
    \"module_path\": str(Path(app.main.__file__).resolve()),
}))
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-c", command],
        check=True,
        capture_output=True,
        cwd=tmp_path,
        text=True,
    )
    payload = json.loads(completed.stdout)

    repository_root = Path(__file__).resolve().parents[2]
    assert payload["distribution_version"] == "0.1.0"
    assert Path(payload["module_path"]) == repository_root / "app" / "main.py"
