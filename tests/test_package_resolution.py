from __future__ import annotations

from pathlib import Path

import scripts


def test_repository_scripts_package_wins_over_site_packages() -> None:
    package_path = Path(scripts.__file__).resolve()
    assert package_path.name == "__init__.py"
    assert package_path.parent.name == "scripts"
    assert (package_path.parent / "research" / "normalize_oddsharvester_sample.py").is_file()

