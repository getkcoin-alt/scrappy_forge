import sys
from pathlib import Path

import pytest

from scrappy_forge.config import Settings
from scrappy_forge.store import Store


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "tests").mkdir()
    (root / "tests/test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\nclass TestAdd(unittest.TestCase):\n"
        "    def test_sum(self): self.assertEqual(add(2, 3), 5)\n"
    )
    return root


@pytest.fixture
def settings(tmp_path):
    return Settings(
        home=tmp_path / "state",
        execution="trusted-local",
        max_steps=8,
        checks=[
            {
                "name": "unit",
                "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                "timeout": 10,
            }
        ],
        allow_tools=["file_write", "file_edit", "verification_run", "run_check"],
    )


@pytest.fixture
def store(settings):
    value = Store(settings.home)
    yield value
    value.close()


@pytest.fixture
def mcp_script():
    return Path(__file__).parent / "fixtures" / "mcp_server.py"
