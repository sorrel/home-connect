"""The package must be importable and expose its version."""
from pathlib import Path

import homeconnect


def test_package_imports():
    assert homeconnect.__version__ == "0.1.0"


def test_src_layout():
    """The shippable package lives under src/, per the workspace convention."""
    root = Path(__file__).resolve().parent.parent
    assert (root / "src" / "homeconnect" / "__init__.py").is_file()
