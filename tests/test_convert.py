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


def test_validation_failure_returns_error_without_writing(monkeypatch, tmp_path, data):
    import convert

    input_path = tmp_path / "input.xml"
    output_path = tmp_path / "output.xml"
    input_path.write_text("<unused/>", encoding="utf-8")
    monkeypatch.setattr(convert, "parse", lambda _path: data)
    monkeypatch.setattr(convert, "_download_xsd", lambda: None)
    monkeypatch.setattr(convert, "_validate", lambda _root: False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["convert.py", str(input_path), str(output_path)],
    )

    assert convert.main() == 1
    assert not output_path.exists()


def test_download_xsd_replaces_cached_copy(monkeypatch, tmp_path):
    import convert

    xsd_path = tmp_path / "documentation" / "eCH-0196-2-2.xsd"
    xsd_path.parent.mkdir()
    xsd_path.write_text("old", encoding="utf-8")
    content = (
        b'<?xml version="1.0"?>'
        b'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>'
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return content

    monkeypatch.setattr(convert, "XSD_PATH", xsd_path)
    monkeypatch.setattr(
        convert.urllib.request, "urlopen", lambda _url, timeout: Response()
    )

    convert._download_xsd()

    assert xsd_path.read_bytes() == content


def test_download_xsd_keeps_cached_copy_on_failure(monkeypatch, tmp_path):
    import convert

    xsd_path = tmp_path / "eCH-0196-2-2.xsd"
    xsd_path.write_text("cached", encoding="utf-8")
    monkeypatch.setattr(convert, "XSD_PATH", xsd_path)

    def fail(_url, timeout):
        raise OSError("offline")

    monkeypatch.setattr(convert.urllib.request, "urlopen", fail)

    convert._download_xsd()

    assert xsd_path.read_text(encoding="utf-8") == "cached"
