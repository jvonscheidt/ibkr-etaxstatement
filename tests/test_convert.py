"""CLI regression tests."""

from __future__ import annotations

import builtins
import importlib
import sys

import pytest


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
    monkeypatch.setattr(convert, "_validate", lambda _root: False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["convert.py", str(input_path), str(output_path)],
    )

    assert convert.main() == 1
    assert not output_path.exists()


def test_multiple_statements_return_clear_error_without_output(
    monkeypatch, tmp_path, capsys
):
    import convert

    input_path = tmp_path / "input.xml"
    output_path = tmp_path / "output.xml"
    pdf_path = tmp_path / "output.pdf"
    input_path.write_text(
        "<FlexQueryResponse><FlexStatements><FlexStatement/>"
        "<FlexStatement/></FlexStatements></FlexQueryResponse>",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "convert.py",
            str(input_path),
            str(output_path),
            "--barcode-pdf",
            str(pdf_path),
        ],
    )

    assert convert.main() == 1
    assert "Multiple FlexStatements" in capsys.readouterr().err
    assert not output_path.exists()
    assert not pdf_path.exists()


@pytest.mark.parametrize("frozen", [False, True])
def test_xsd_is_found_beside_application_before_working_directory(
    monkeypatch, tmp_path, frozen
):
    import convert

    application = tmp_path / "app"
    working = tmp_path / "work"
    extraction = tmp_path / "_MEI123"
    for directory in (application, working, extraction):
        (directory / "documentation").mkdir(parents=True)
        (directory / "documentation" / "eCH-0196-2-2.xsd").write_text(
            "<schema/>", encoding="utf-8"
        )
    monkeypatch.chdir(working)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "executable", str(application / "converter.exe"))
    monkeypatch.setattr(
        convert, "__file__", str((extraction if frozen else application) / "convert.py")
    )

    assert convert._find_xsd() == application / "documentation" / "eCH-0196-2-2.xsd"


def test_frozen_xsd_falls_back_to_working_directory(monkeypatch, tmp_path):
    import convert

    documentation = tmp_path / "documentation"
    documentation.mkdir()
    path = documentation / "eCH-0196-2-2.xsd"
    path.write_text("<schema/>", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app" / "converter.exe"))

    assert convert._find_xsd() == path


def test_frozen_validation_uses_external_xsd(monkeypatch, tmp_path, capsys):
    import xml.etree.ElementTree as ET

    import convert

    pytest.importorskip("lxml.etree")
    documentation = tmp_path / "app" / "documentation"
    documentation.mkdir(parents=True)
    (documentation / "eCH-0196-2-2.xsd").write_text(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="valid"/></xs:schema>',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app" / "converter.exe"))
    monkeypatch.setattr(convert, "__file__", str(tmp_path / "_MEI123" / "convert.py"))

    assert not convert._validate(ET.Element("invalid"))
    assert "XSD validation FAILED" in capsys.readouterr().out


def test_missing_external_xsd_is_explicit(monkeypatch, tmp_path, capsys):
    import xml.etree.ElementTree as ET

    import convert

    pytest.importorskip("lxml.etree")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "converter.exe"))

    assert convert._find_xsd() is None
    assert convert._validate(ET.Element("unused"))
    assert "skipping validation" in capsys.readouterr().out
