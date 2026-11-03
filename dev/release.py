#!/usr/bin/env -S uv run --script
"""Build, check, and upload the tempo-agents package (AGENT_PLAN.md section 6, Publishing).

`dev/release.py` builds a clean `dist/`, refuses an sdist entry outside the allowlist, smoke-tests the
wheel in a fresh venv (`tempo init`, `tempo render --check`), then runs `uv publish`. Without a PyPI
token in `UV_PUBLISH_TOKEN`, it asks for one and saves it as the `export` line in ~/.bashrc.
`--check` stops before the upload.
"""

import argparse
import getpass
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
# Mirrors [tool.hatch.build.targets.sdist] include, plus what the build backend always adds.
SDIST_ALLOWED = {"tempo", "README.md", "pyproject.toml", "PKG-INFO", ".gitignore"}
PLACEHOLDER = "pypi-PASTE-YOUR-TOKEN-HERE"
BASHRC = Path.home() / ".bashrc"
EXPORT = re.compile(r"^export UV_PUBLISH_TOKEN=.*$", re.MULTILINE)


def run(argv: list[str], cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print("$", " ".join(argv), flush=True)
    subprocess.run(argv, cwd=cwd, check=True, env=env)


def fail(message: str, road: str) -> None:
    sys.exit(f"release: {message}\n  do instead: {road}")


def build() -> tuple[Path, Path]:
    shutil.rmtree(DIST, ignore_errors=True)
    run(["uv", "build"])
    wheels, sdists = sorted(DIST.glob("*.whl")), sorted(DIST.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        fail(f"expected one wheel and one sdist in dist/, found {[p.name for p in DIST.iterdir()]}",
             "check the build output above")
    return wheels[0], sdists[0]


def check_sdist(sdist: Path) -> None:
    with tarfile.open(sdist) as tar:
        top = {name.split("/")[1] for name in tar.getnames() if name.count("/") >= 1}
    if stray := sorted(top - SDIST_ALLOWED):
        fail(f"the sdist carries entries outside the allowlist: {stray}",
             "fix [tool.hatch.build.targets.sdist] include in pyproject.toml")
    print(f"sdist entries: {sorted(top)}")


def smoke(wheel: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        venv, repo = Path(tmp) / "venv", Path(tmp) / "repo"
        run(["uv", "venv", "-q", str(venv)])
        run(["uv", "pip", "install", "-q", "-p", str(venv), str(wheel)])
        repo.mkdir()
        run(["git", "init", "-q", "-b", "main"], cwd=repo)
        tempo = str(venv / "bin" / "tempo")
        run([tempo, "init"], cwd=repo)
        run([tempo, "render", "--check"], cwd=repo)


def valid(token: str) -> bool:
    return token.startswith("pypi-") and token != PLACEHOLDER


def token() -> str:
    """The env token, else one typed now and saved as the ~/.bashrc export line."""
    if valid(found := os.environ.get("UV_PUBLISH_TOKEN", "")):
        return found
    typed = getpass.getpass("PyPI token (starts with pypi-, input hidden): ").strip()
    if not valid(typed):
        fail("that is not a PyPI token", "copy the whole token from https://pypi.org/manage/account/token/")
    line = f'export UV_PUBLISH_TOKEN="{typed}"'
    text = BASHRC.read_text() if BASHRC.is_file() else ""
    if EXPORT.search(text):
        text = EXPORT.sub(lambda _: line, text, count=1)
    else:
        text = f"{text.rstrip()}\n\n# PyPI upload token for `uv publish`\n{line}\n"
    BASHRC.write_text(text)
    print(f"saved the token to {BASHRC}")
    return typed


def publish(key: str) -> None:
    run(["uv", "publish"], env={**os.environ, "UV_PUBLISH_TOKEN": key})


def main() -> None:
    parser = argparse.ArgumentParser(prog="dev/release.py")
    parser.add_argument("--check", action="store_true", help="build and check, but do not upload")
    args = parser.parse_args()
    key = None if args.check else token()  # ask before the build, not after it
    wheel, sdist = build()
    check_sdist(sdist)
    smoke(wheel)
    if args.check:
        print("release: checks passed; not uploaded (--check)")
        return
    assert key is not None
    publish(key)
    print("release: uploaded; see https://pypi.org/project/tempo-agents/")


if __name__ == "__main__":
    main()
