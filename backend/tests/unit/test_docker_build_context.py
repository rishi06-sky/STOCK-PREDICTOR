"""The build context is part of the deployment contract.

Both Dockerfiles copy a directory wholesale (`COPY backend/ .`, `COPY
frontend/ .`). Without a .dockerignore that pulls the host's build artefacts
into the image: a 1.2GB virtualenv, the frontend's node_modules copied over
the top of the `npm ci` layer, and -- worst -- the trained model store, whose
artefact checksums will not match the registry of whatever database the image
is deployed against, so predictions fail closed on a checksum mismatch.

The file is easy to delete and the damage is invisible until deployment, so
its contents are asserted here rather than left to review.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
DOCKER_DIR = REPO_ROOT / "docker"

# The backend image copies only `backend/`, so inside the container there is no
# repo root to inspect. Skip there rather than fail; when the checkout *is*
# present (dev machines, CI) every assertion below still runs, so the guard
# against deleting .dockerignore is preserved where it can actually fire.
pytestmark = pytest.mark.skipif(
    not DOCKER_DIR.is_dir(),
    reason="not a source checkout (running inside the built image)",
)

# Paths that must never reach the build context, with why they matter.
MUST_EXCLUDE = [
    ("backend/.venv/lib/python3.11/site-packages/x.py", "host virtualenv"),
    ("frontend/node_modules/react/index.js", "host node_modules"),
    ("frontend/.next/server/app/page.js", "host Next.js build output"),
    ("backend/model_store/direction_5d/v1.joblib", "trained model artefacts"),
    ("backend/app/__pycache__/main.cpython-311.pyc", "compiled bytecode"),
    ("backend/.pytest_cache/CACHEDIR.TAG", "pytest cache"),
    ("backend/coverage.xml", "coverage output"),
    (".git/config", "git metadata"),
    (".env", "local secrets"),
]

# Paths the image genuinely needs.
MUST_INCLUDE = [
    "backend/app/main.py",
    "backend/requirements.txt",
    "backend/alembic/env.py",
    "frontend/package.json",
    "frontend/app/page.tsx",
    ".env.example",
]


def _patterns() -> list[str]:
    return [
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _is_excluded(path: str) -> bool:
    """Approximate Docker's matcher: last matching pattern wins, ! re-includes."""
    excluded = False
    for pattern in _patterns():
        negate = pattern.startswith("!")
        raw = pattern[1:] if negate else pattern
        candidates = [raw]
        if raw.startswith("**/"):
            # `**/x` also matches `x` at the root.
            candidates.append(raw[3:])
        hit = any(
            fnmatch.fnmatch(path, c)
            or fnmatch.fnmatch(path, f"{c}/*")
            or any(fnmatch.fnmatch(part, c) for part in Path(path).parts)
            for c in candidates
        )
        if hit:
            excluded = not negate
    return excluded


def test_dockerignore_exists():
    assert DOCKERIGNORE.is_file(), (
        ".dockerignore is missing. Both Dockerfiles copy whole directories, so "
        "without it the host virtualenv, node_modules and model store end up "
        "inside the image."
    )


@pytest.mark.parametrize("path,why", MUST_EXCLUDE, ids=[w for _, w in MUST_EXCLUDE])
def test_host_artefacts_stay_out_of_the_build_context(path, why):
    assert _is_excluded(path), f"{why} ({path}) would be copied into the image"


@pytest.mark.parametrize("path", MUST_INCLUDE)
def test_application_sources_reach_the_build_context(path):
    assert not _is_excluded(path), f"{path} is needed in the image but is excluded"


def test_the_example_env_is_kept_even_though_env_files_are_excluded():
    """.env is excluded; .env.example is the documented template and must stay."""
    assert _is_excluded(".env")
    assert not _is_excluded(".env.example")


def _dockerfile(name: str) -> str:
    return (DOCKER_DIR / name).read_text()


def test_backend_image_installs_the_openmp_runtime():
    """LightGBM dlopens libgomp.so.1; without it `import lightgbm` raises OSError.

    python:3.11-slim does not ship it, so dropping this package turns every
    prediction into an import error at runtime rather than a build failure.
    """
    assert "libgomp1" in _dockerfile("Dockerfile.backend")


def test_backend_image_installs_curl_for_its_healthcheck():
    """The HEALTHCHECK shells out to curl, which slim does not include."""
    backend = _dockerfile("Dockerfile.backend")
    assert "curl" in backend
    assert "HEALTHCHECK" in backend


@pytest.mark.parametrize("name", ["Dockerfile.backend", "Dockerfile.frontend"])
def test_the_proxy_ca_secret_is_optional(name):
    """Builds must succeed without `--secret id=proxy_ca`.

    The CA is only needed behind a TLS-intercepting proxy. Every RUN that
    mounts it has to test the file before exporting a CA variable -- pointing
    pip or node at a path that does not exist breaks an ordinary build.
    """
    text = _dockerfile(name)
    for line in text.splitlines():
        if "type=secret,id=proxy_ca" in line:
            target = line.split("target=")[1].split()[0].rstrip(" \\")
            assert f"[ -s {target} ]" in text, (
                f"{name} mounts the proxy CA at {target} without guarding on its "
                "presence, so a build without the secret would break"
            )
