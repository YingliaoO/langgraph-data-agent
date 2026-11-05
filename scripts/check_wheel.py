"""Reject accidental publication of non-backend assets in a built wheel."""

import sys
from pathlib import Path
from zipfile import ZipFile


def check_wheel(path: Path) -> None:
    with ZipFile(path) as archive:
        names = archive.namelist()
    assert "multisource_data_agent/main.py" in names, "Application entry point missing"
    unexpected = [
        name for name in names
        if name.split("/", 1)[0] != "multisource_data_agent"
        and not name.split("/", 1)[0].endswith(".dist-info")
    ]
    assert not unexpected, f"Unexpected wheel assets: {unexpected}"
    print(f"Wheel contents verified: {path.name} ({len(names)} files)")


if __name__ == "__main__":
    check_wheel(Path(sys.argv[1]))
