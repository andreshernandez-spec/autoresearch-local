"""The type checker, when it is on this machine.

Records are typed (arl/record.py), so a field that is misspelt anywhere in the
package is an error here, whether or not another test happens to run that line.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_the_package_type_checks():
    pyright = shutil.which("pyright")
    if pyright is None:
        pytest.skip("pyright is not installed")
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run([pyright, "src"], cwd=root, capture_output=True, text=True, timeout=600)
    assert out.returncode == 0, out.stdout[-4000:]
