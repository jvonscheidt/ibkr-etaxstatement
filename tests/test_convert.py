"""CLI regression tests."""

from __future__ import annotations

import builtins
import importlib
import sys


def test_xml_only_import_does_not_load_barcode_dependencies(monkeypatch):
    blocked = ("PIL", "pdf417gen", "pypdf", "reportlab")
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.startswith(blocked):
            raise AssertionError(f"barcode dependency imported: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    sys.modules.pop("convert", None)
    sys.modules.pop("src.generate_barcode_pdf", None)

    importlib.import_module("convert")


def test_validation_failure_returns_error_without_writing(
    monkeypatch, tmp_path, data
):
    import convert

    input_path = tmp_path / "input.xml"
    output_path = tmp_path / "output.xml"
    input_path.write_text("<unused/>", encoding="utf-8")
    monkeypatch.setattr(convert, "parse", lambda _path: data)
    monkeypatch.setattr(convert, "_validate", lambda _root: False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["convert.py", str(input_path), str(output_path)],
    )

    assert convert.main() == 1
    assert not output_path.exists()
