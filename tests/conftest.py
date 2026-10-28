import json
from pathlib import Path

import pytest

from sheetaudit import audit


@pytest.fixture(scope="session")
def samples():
    directory = Path(__file__).resolve().parents[1] / "samples"
    truth = json.loads((directory / "ground_truth.json").read_text(encoding="utf-8"))
    return directory, truth, {name: audit(directory / name) for name in truth}
