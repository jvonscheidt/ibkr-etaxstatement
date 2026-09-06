"""CLI regression tests."""

from __future__ import annotations

import builtins
import importlib
import io
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

    monkeypatch.setattr(convert, "_find_xsd", lambda: xsd_path)
    monkeypatch.setattr(
        convert.urllib.request, "urlopen", lambda _url, timeout: Response()
    )

    convert._download_xsd()

    assert xsd_path.read_bytes() == content


def test_download_xsd_keeps_cached_copy_on_failure(monkeypatch, tmp_path):
    import convert

    xsd_path = tmp_path / "eCH-0196-2-2.xsd"
    xsd_path.write_text("cached", encoding="utf-8")
    monkeypatch.setattr(convert, "_find_xsd", lambda: xsd_path)

    def fail(_url, timeout):
        raise OSError("offline")

    monkeypatch.setattr(convert.urllib.request, "urlopen", fail)

    convert._download_xsd()

    assert xsd_path.read_text(encoding="utf-8") == "cached"


def test_download_xsd_caches_dependencies_and_retains_qname_namespaces(
    monkeypatch, tmp_path
):
    import convert

    etree = pytest.importorskip("lxml.etree")
    xsd_path = tmp_path / "documentation" / "eCH-0196-2-2.xsd"
    dependency_url = "https://www.ech.ch/xmlns/test/1/test.xsd"
    common_url = "https://www.ech.ch/xmlns/test/1/common.xsd"
    schemas = {
        convert.XSD_URL: (
            b'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:t="urn:test">'
            b'<xs:import namespace="urn:test" '
            b'schemaLocation="http://www.ech.ch/xmlns/test/1/test.xsd"/>'
            b'<xs:element name="valid" type="t:TextType"/></xs:schema>'
        ),
        dependency_url: (
            b'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:test">'
            b'<xs:include schemaLocation="common.xsd"/></xs:schema>'
        ),
        common_url: (
            b'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:test">'
            b'<xs:simpleType name="TextType"><xs:restriction base="xs:string"/>'
            b"</xs:simpleType></xs:schema>"
        ),
    }
    requests = []

    def download(url, timeout):
        requests.append(url)
        assert timeout == 30
        return io.BytesIO(schemas[url])

    monkeypatch.setattr(convert, "_find_xsd", lambda: xsd_path)
    monkeypatch.setattr(convert.urllib.request, "urlopen", download)

    convert._download_xsd()

    assert set(requests) == set(schemas)
    root = etree.parse(str(xsd_path))
    location = (
        root.getroot().find(f"{{{convert.XSD_NAMESPACE}}}import").get("schemaLocation")
    )
    dependency = xsd_path.parent / location
    assert dependency.is_file()
    assert (dependency.parent / "common.xsd").is_file()
    schema = etree.XMLSchema(root)
    assert schema.validate(etree.fromstring(b"<valid>value</valid>"))


def test_failed_dependency_download_preserves_cached_schema(monkeypatch, tmp_path):
    import convert

    xsd_path = tmp_path / "eCH-0196-2-2.xsd"
    xsd_path.write_bytes(b"cached schema")
    dependency = tmp_path / "dependency.xsd"
    dependency.write_bytes(b"cached dependency")

    def download(url, timeout):
        if url != convert.XSD_URL:
            raise OSError("dependency unavailable")
        return io.BytesIO(
            b'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            b'<xs:include schemaLocation="dependency.xsd"/></xs:schema>'
        )

    monkeypatch.setattr(convert, "_find_xsd", lambda: xsd_path)
    monkeypatch.setattr(convert.urllib.request, "urlopen", download)

    convert._download_xsd()

    assert xsd_path.read_bytes() == b"cached schema"
    assert dependency.read_bytes() == b"cached dependency"


def test_failed_schema_install_preserves_complete_cached_graph(monkeypatch, tmp_path):
    import convert

    etree = pytest.importorskip("lxml.etree")
    xsd_path = tmp_path / "eCH-0196-2-2.xsd"
    dependency = tmp_path / "dependency.xsd"
    old_root = (
        b'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        b'<xs:include schemaLocation="dependency.xsd"/>'
        b'<xs:element name="valid" type="OldType"/></xs:schema>'
    )
    old_dependency = (
        b'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        b'<xs:simpleType name="OldType"><xs:restriction base="xs:string"/>'
        b"</xs:simpleType></xs:schema>"
    )
    xsd_path.write_bytes(old_root)
    dependency.write_bytes(old_dependency)

    def download(url, timeout):
        content = old_root if url == convert.XSD_URL else old_dependency
        return io.BytesIO(content.replace(b"OldType", b"NewType"))

    replace = convert.os.replace

    def fail_root_replacement(source, destination):
        if destination == xsd_path:
            raise PermissionError("cached root is locked")
        replace(source, destination)

    monkeypatch.setattr(convert, "_find_xsd", lambda: xsd_path)
    monkeypatch.setattr(convert.urllib.request, "urlopen", download)
    monkeypatch.setattr(convert.os, "replace", fail_root_replacement)

    convert._download_xsd()

    assert xsd_path.read_bytes() == old_root
    assert dependency.read_bytes() == old_dependency
    assert set(tmp_path.iterdir()) == {xsd_path, dependency}
    schema = etree.XMLSchema(etree.parse(str(xsd_path)))
    assert schema.validate(etree.fromstring(b"<valid>value</valid>"))


def test_frozen_download_uses_executable_directory_not_extraction(
    monkeypatch, tmp_path
):
    import convert

    content = b'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>'
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app" / "converter.exe"))
    monkeypatch.setattr(convert, "__file__", str(tmp_path / "_MEI123" / "convert.py"))
    monkeypatch.setattr(
        convert.urllib.request, "urlopen", lambda _url, timeout: io.BytesIO(content)
    )

    convert._download_xsd()

    assert convert._find_xsd() == (
        tmp_path / "app" / "documentation" / "eCH-0196-2-2.xsd"
    )
    assert not (tmp_path / "_MEI123").exists()


def test_download_refreshes_existing_working_directory_cache(monkeypatch, tmp_path):
    import convert

    xsd_path = tmp_path / "documentation" / "eCH-0196-2-2.xsd"
    xsd_path.parent.mkdir()
    xsd_path.write_bytes(b"old")
    content = b'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>'
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app" / "converter.exe"))
    monkeypatch.setattr(
        convert.urllib.request, "urlopen", lambda _url, timeout: io.BytesIO(content)
    )

    convert._download_xsd()

    assert xsd_path.read_bytes() == content
    assert not (tmp_path / "app").exists()


def test_multiple_statements_return_clear_error_without_output(
    monkeypatch, tmp_path, capsys
):
    import convert

    monkeypatch.setattr(convert, "_download_xsd", lambda: None)
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
