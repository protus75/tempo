import importlib
import re
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_tempo_package_imports():
    tempo = importlib.import_module("tempo")
    assert Path(tempo.__file__).resolve().parent == PYPROJECT.parent / "tempo"


def test_pyproject_pins_python_floor_and_runtime_deps():
    project = tomllib.loads(PYPROJECT.read_text())["project"]
    assert project["requires-python"] == ">=3.14"
    names = {re.match(r"[A-Za-z0-9._-]+", dep).group().lower() for dep in project["dependencies"]}
    # Section 0 runtime set: exactly these non-stdlib deps, nothing accreted.
    assert names == {"pyyaml", "pydantic"}
