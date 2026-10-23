"""Testy struktury integracji.

- mapa jest encją image (image.py), bez kamery,
- kopia narwal_client w integracji jest identyczna z biblioteką.
"""

from __future__ import annotations

import filecmp
from pathlib import Path

import tests.ha_stubs

tests.ha_stubs.install()


ROOT = Path(__file__).resolve().parent.parent
INTEGRATION = ROOT / "custom_components" / "narwal"


class TestMapPlatform:
    """Mapa jest encją image, bez kamery."""

    def test_platform_image_in_platforms(self) -> None:
        from custom_components.narwal.const import PLATFORMS
        from homeassistant.const import Platform

        assert Platform.IMAGE in PLATFORMS
        assert Platform.CAMERA not in PLATFORMS

    def test_image_py_exists_and_camera_py_not(self) -> None:
        assert (INTEGRATION / "image.py").is_file()
        assert not (INTEGRATION / "camera.py").is_file()

    def test_map_entity_inherits_image_entity(self) -> None:
        import ast

        tree = ast.parse((INTEGRATION / "image.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "NarwalMapImage":
                bases = [getattr(b, "id", getattr(b, "attr", "")) for b in node.bases]
                assert "ImageEntity" in bases
                return
        raise AssertionError("brak klasy NarwalMapImage w image.py")


class TestCLN02NarwalClientSync:
    """CLN-02: narwal_client embedded copy matches canonical copy."""

    CANONICAL = ROOT / "narwal_client"
    EMBEDDED = INTEGRATION / "narwal_client"

    def test_canonical_exists(self) -> None:
        """Canonical narwal_client/ directory exists."""
        assert self.CANONICAL.is_dir()

    def test_embedded_exists(self) -> None:
        """Embedded narwal_client/ directory exists."""
        assert self.EMBEDDED.is_dir()

    def test_same_file_list(self) -> None:
        """Both copies have the same set of .py files."""
        canonical_py = sorted(
            f.name for f in self.CANONICAL.rglob("*.py")
            if "__pycache__" not in str(f)
        )
        embedded_py = sorted(
            f.name for f in self.EMBEDDED.rglob("*.py")
            if "__pycache__" not in str(f)
        )
        assert canonical_py == embedded_py, (
            f"File list mismatch:\n"
            f"  Canonical only: {set(canonical_py) - set(embedded_py)}\n"
            f"  Embedded only: {set(embedded_py) - set(canonical_py)}"
        )

    def test_files_byte_identical(self) -> None:
        """All .py files are byte-for-byte identical between copies."""
        canonical_files = sorted(
            f for f in self.CANONICAL.rglob("*.py")
            if "__pycache__" not in str(f)
        )
        for canonical_file in canonical_files:
            relative = canonical_file.relative_to(self.CANONICAL)
            embedded_file = self.EMBEDDED / relative
            assert embedded_file.is_file(), f"Missing embedded file: {relative}"
            assert filecmp.cmp(
                str(canonical_file), str(embedded_file), shallow=False
            ), f"Content mismatch: {relative}"
