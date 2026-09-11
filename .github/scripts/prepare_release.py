"""Validate release versions and stage checksum-specific Windows release assets."""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IDENTIFIER = "jvonscheidt.ibkr-etaxstatement"


def validate_version(root: Path, tag: str) -> str:
    if not re.fullmatch(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", tag):
        raise ValueError(f"Expected a stable version tag such as v0.3.1, got {tag!r}")
    version = tag[1:]
    tree = ast.parse((root / "convert.py").read_text(encoding="utf-8"))
    versions = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        )
    ]
    if versions != [version]:
        raise ValueError(f"convert.py version does not match {tag}")
    windows = (root / "packaging" / "windows-version-info.txt").read_text(
        encoding="utf-8"
    )
    for field in ("FileVersion", "ProductVersion"):
        if f'StringStruct("{field}", "{version}")' not in windows:
            raise ValueError(f"Windows {field} does not match {tag}")
    numeric_version = (*map(int, version.split(".")), 0)
    for field in ("filevers", "prodvers"):
        match = re.search(rf"\b{field}\s*=\s*(\([^)]*\))", windows)
        if match is None or ast.literal_eval(match[1]) != numeric_version:
            raise ValueError(f"Windows {field} does not match {tag}")
    return version


def stage_release(root: Path, tag: str, installer: Path, output: Path) -> None:
    version = validate_version(root, tag)
    if installer.name != "ibkr-etaxstatement.exe":
        raise ValueError("Expected an installer named ibkr-etaxstatement.exe")
    with installer.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("Installer is not a Windows executable")
        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    manifests = {}
    for kind in ("installer", "locale.en-US", ""):
        name = f"{IDENTIFIER}{'.' + kind if kind else ''}.yaml"
        template = (
            root / "packaging" / "winget" / "templates" / f"{name}.in"
        ).read_text(encoding="utf-8")
        manifests[name] = template.replace("@VERSION@", version).replace(
            "@SHA256@", digest.upper()
        )
        if re.search(r"@[A-Z0-9_]+@", manifests[name]):
            raise ValueError(f"Unresolved template placeholder in {name}")
    # Refuse stale output so a retried build cannot ship unrelated release files.
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(installer, output / installer.name)
    (output / "SHA256SUMS").write_text(
        f"{digest}  {installer.name}\n", encoding="ascii"
    )
    directory = output / "winget"
    directory.mkdir()
    for name, content in manifests.items():
        (directory / name).write_text(content, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (args.installer is None) != (args.output is None):
        parser.error("--installer and --output must be provided together")
    if args.installer is None:
        validate_version(ROOT, args.tag)
    else:
        stage_release(ROOT, args.tag, args.installer, args.output)


if __name__ == "__main__":
    main()
