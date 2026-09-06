"""Release staging must bind matching source versions to the built executable."""

import hashlib
import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prepare_release", ROOT / ".github" / "scripts" / "prepare_release.py"
)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for name in ("convert.py", "README.md"):
        shutil.copyfile(ROOT / name, root / name)
    shutil.copytree(ROOT / "packaging", root / "packaging")
    return root


def test_current_release_versions_match():
    import convert

    assert release.validate_version(ROOT, f"v{convert.__version__}") == "0.3.1"


@pytest.mark.parametrize(
    "tag", ["0.3.1", "v0.3.1-rc1", "v0.3.1\n", "v00.3.1", "v0.3.1/other", "v0.3.0"]
)
def test_invalid_or_mismatched_tag_is_rejected(tag):
    with pytest.raises(ValueError):
        release.validate_version(ROOT, tag)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        ("convert.py", '__version__ = "0.3.1"', '__version__ = "0.3.0"'),
        ("README.md", "Version: **0.3.1**.", "Version: **0.3.0**."),
        (
            "packaging/windows-version-info.txt",
            'StringStruct("FileVersion", "0.3.1")',
            'StringStruct("FileVersion", "0.3.0")',
        ),
        (
            "packaging/windows-version-info.txt",
            'StringStruct("ProductVersion", "0.3.1")',
            'StringStruct("ProductVersion", "0.3.0")',
        ),
        (
            "packaging/windows-version-info.txt",
            "filevers=(0, 3, 1, 0)",
            "filevers=(0, 3, 0, 0)",
        ),
        (
            "packaging/windows-version-info.txt",
            "prodvers=(0, 3, 1, 0)",
            "prodvers=(0, 3, 0, 0)",
        ),
    ],
)
def test_all_version_surfaces_are_required(source, path, old, new):
    file = source / Path(path)
    content = file.read_text(encoding="utf-8")
    assert old in content
    file.write_text(content.replace(old, new), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        release.validate_version(source, "v0.3.1")


@pytest.mark.parametrize("payload", [b"MZfirst build", b"MZsecond build"])
def test_staging_hashes_exact_installer_and_renders_all_manifests(tmp_path, payload):
    installer = tmp_path / "ibkr-etaxstatement.exe"
    installer.write_bytes(payload)
    output = tmp_path / "release"

    release.stage_release(ROOT, "v0.3.1", installer, output)

    digest = hashlib.sha256(payload).hexdigest()
    assert (output / installer.name).read_bytes() == payload
    assert (output / "SHA256SUMS").read_text() == f"{digest}  {installer.name}\n"
    manifests = sorted((output / "winget").glob("*.yaml"))
    assert len(manifests) == 3
    for manifest in manifests:
        content = manifest.read_text()
        assert "PackageVersion: 0.3.1\n" in content
        assert f"PackageIdentifier: {release.IDENTIFIER}\n" in content
        assert "@VERSION@" not in content
        assert "@SHA256@" not in content
    content = (output / "winget" / f"{release.IDENTIFIER}.installer.yaml").read_text()
    assert f"InstallerSha256: {digest.upper()}\n" in content
    assert "/releases/download/v0.3.1/ibkr-etaxstatement.exe" in content


def test_staging_rejects_non_executable_without_creating_output(tmp_path):
    installer = tmp_path / "ibkr-etaxstatement.exe"
    installer.write_bytes(b"error page")
    output = tmp_path / "release"
    with pytest.raises(ValueError, match="not a Windows executable"):
        release.stage_release(ROOT, "v0.3.1", installer, output)
    assert not output.exists()


def test_staging_never_overwrites_existing_output(tmp_path):
    installer = tmp_path / "ibkr-etaxstatement.exe"
    installer.write_bytes(b"MZnew")
    output = tmp_path / "release"
    output.mkdir()
    previous = output / installer.name
    previous.write_bytes(b"MZprevious")
    with pytest.raises(FileExistsError):
        release.stage_release(ROOT, "v0.3.1", installer, output)
    assert previous.read_bytes() == b"MZprevious"


def test_missing_template_does_not_leave_partial_release(source, tmp_path):
    installer = tmp_path / "ibkr-etaxstatement.exe"
    installer.write_bytes(b"MZexecutable")
    template = source / "packaging" / "winget" / "templates"
    (template / f"{release.IDENTIFIER}.yaml.in").unlink()
    output = tmp_path / "release"
    with pytest.raises(FileNotFoundError):
        release.stage_release(source, "v0.3.1", installer, output)
    assert not output.exists()
