"""Guards the platform-conditional lines in requirements.txt.

pip-tools resolves requirements.txt against the platform it was compiled on
(this repo compiles it on macOS) and does not carry over the sys_platform
markers that some packages declare on their own conditional dependencies.
Two are known and hand-patched onto requirements.txt after generation (see
the comment at the top of that file):

  - pyobjc-core, pyobjc-framework-cocoa, pyobjc-framework-quartz (pystray's
    macOS tray backend) need `sys_platform == "darwin"`. Without it, `pip
    install -r requirements.txt` fails outright on Linux, where PyObjC does
    not build.
  - python-xlib (pystray's Linux tray backend) needs
    `sys_platform == "linux"`. Without it, the package is absent from the
    lock entirely and a Linux install floats it to whatever version PyPI
    serves that day, defeating the point of pinning.

This list is not closed. Any platform-conditional transitive dependency is
at risk the same way -- for example click depends on colorama only on
Windows; it is not covered here because no Windows build exists yet, but
watch for it when one does.

If someone re-runs `pip-compile -o requirements.txt requirements.in`
without knowing about the hand-patch, the markers silently disappear again
-- correct behaviour for pip-compile on its own terms, wrong behaviour for
a lock that has to install on more than one platform. A comment does not
survive that; this test does.
"""

import re
from pathlib import Path

import pytest

REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"

# package -> the exact sys_platform marker its requirements.txt line must
# carry.
EXPECTED_MARKERS = {
    "pyobjc-core": 'sys_platform == "darwin"',
    "pyobjc-framework-cocoa": 'sys_platform == "darwin"',
    "pyobjc-framework-quartz": 'sys_platform == "darwin"',
    "python-xlib": 'sys_platform == "linux"',
}


def _lines() -> list[str]:
    return REQUIREMENTS.read_text().splitlines()


@pytest.mark.parametrize("package,marker", sorted(EXPECTED_MARKERS.items()))
def test_platform_conditional_package_carries_its_marker(package: str, marker: str) -> None:
    pinned = re.compile(rf"^{re.escape(package)}==\S+$")
    marked = re.compile(rf"^{re.escape(package)}==\S+ ; {re.escape(marker)}$")

    matches = [line for line in _lines() if pinned.match(line) or marked.match(line)]
    assert matches, (
        f"{package} is missing from requirements.txt entirely. This "
        f"usually means requirements.txt was regenerated with pip-compile: "
        f"it resolves the lock against the compiling host only, so a "
        f"platform-conditional dependency can vanish outright if it "
        f"doesn't apply there. Re-add the line by hand as "
        f'`{package}==<version> ; {marker}` -- this pip-tools version '
        f"(7.6.1, no --universal flag) cannot resolve markers for other "
        f"platforms itself."
    )
    assert any(marked.match(line) for line in matches), (
        f"{package} is present in requirements.txt but missing its "
        f'`; {marker}` marker. requirements.txt was likely regenerated '
        f"with pip-compile, which silently drops the sys_platform markers "
        f"that pystray declares on its own conditional dependencies. "
        f'Re-add `; {marker}` to the {package} line by hand -- this '
        f"pip-tools version (7.6.1, no --universal flag) cannot resolve "
        f"markers for other platforms itself."
    )
