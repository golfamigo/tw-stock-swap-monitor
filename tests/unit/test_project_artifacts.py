import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_dockerignore_excludes_credentials_and_local_artifacts() -> None:
    """The Docker build context omits credentials and generated local files."""
    dockerignore = PROJECT_ROOT / ".dockerignore"

    assert dockerignore.is_file()

    patterns = set(dockerignore.read_text(encoding="utf-8").splitlines())
    assert {
        ".env",
        ".env.*",
        "!.env.example",
        ".local.env",
        ".git",
        ".venv",
        "__pycache__/",
        "*.py[cod]",
        ".coverage",
        ".mypy_cache/",
        ".pytest_cache/",
        ".ruff_cache/",
        "tests/",
    } <= patterns


def test_gitignore_excludes_environment_files_except_the_example() -> None:
    """Only the committed environment-variable name template remains trackable."""
    patterns = set((PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())

    assert {".env", ".env.*", "!.env.example", ".local.env"} <= patterns


def test_ignore_files_exclude_wheel_build_artifacts() -> None:
    """Local wheel builds do not dirty Git status or enter the Docker context."""
    gitignore_patterns = set((PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
    dockerignore_patterns = set(
        (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    )

    assert "build/" in gitignore_patterns
    assert "build/" in dockerignore_patterns


def test_container_uses_an_unprivileged_runtime_user() -> None:
    """The container's final runtime process does not run as root."""
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "USER appuser" in dockerfile


def test_setuptools_discovers_application_packages_explicitly() -> None:
    """Packaging configuration explicitly includes the application package tree."""
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    assert pyproject["tool"]["setuptools"]["packages"]["find"]["include"] == ["app*"]
