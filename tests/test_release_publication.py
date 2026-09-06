"""Exercise release publication with mocked GitHub APIs, never live mutations."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")

HARNESS = r"""
const publish = require(process.env.PUBLISH_SCRIPT)
const scenario = JSON.parse(process.env.SCENARIO)
const calls = []
const methods = ["listReleases", "generateReleaseNotes", "createRelease",
                 "deleteReleaseAsset", "uploadReleaseAsset", "updateRelease"]
const repos = Object.fromEntries(methods.map(name => [name, async args => {
  calls.push({name, args})
  if (name === scenario.fail) throw new Error("simulated API failure")
  if (name === "generateReleaseNotes") return {data: {body: "release notes"}}
  if (name === "createRelease") return {data: {id: 42, draft: true, assets: []}}
  return {data: {}}
}]))
const github = {
  rest: {repos},
  paginate: async (method, args) => {
    await method(args)
    return scenario.releases || []
  },
}
const context = {repo: {owner: "example", repo: "project"}, sha: "tagged-commit"}
publish({github, context}).then(
  () => console.log(JSON.stringify({calls})),
  error => console.log(JSON.stringify({calls, error: error.message})),
)
"""


@pytest.fixture
def assets(tmp_path):
    (tmp_path / "ibkr-etaxstatement.exe").write_bytes(b"MZexecutable")
    (tmp_path / "SHA256SUMS").write_text("checksum")
    directory = tmp_path / "winget"
    directory.mkdir()
    for kind in ("installer", "locale.en-US", ""):
        name = f"jvonscheidt.ibkr-etaxstatement{'.' + kind if kind else ''}.yaml"
        (directory / name).write_text("manifest")
    return tmp_path


def run_publish(assets, scenario):
    environment = {
        **os.environ,
        "PUBLISH_SCRIPT": str(ROOT / ".github" / "scripts" / "publish-release.js"),
        "RELEASE_TAG": "v0.3.1",
        "RELEASE_ASSET_DIR": str(assets),
        "SCENARIO": json.dumps(scenario),
    }
    result = subprocess.run(
        [NODE, "-e", HARNESS],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_publishes_only_after_all_assets_are_uploaded(assets):
    result = run_publish(assets, {})
    assert "error" not in result
    calls = result["calls"]
    assert [call["name"] for call in calls] == [
        "listReleases",
        "generateReleaseNotes",
        "createRelease",
        *(["uploadReleaseAsset"] * 5),
        "updateRelease",
    ]
    created = calls[2]["args"]
    assert created["draft"] is True
    assert created["target_commitish"] == "tagged-commit"
    assert calls[-1]["args"]["draft"] is False


def test_retry_resumes_matching_draft_and_replaces_partial_assets(assets):
    result = run_publish(
        assets,
        {
            "releases": [
                {"tag_name": "v0.3.0", "draft": False},
                {
                    "tag_name": "v0.3.1",
                    "draft": True,
                    "id": 99,
                    "assets": [{"name": "ibkr-etaxstatement.exe", "id": 123}],
                },
            ]
        },
    )
    assert "error" not in result
    calls = result["calls"]
    assert "createRelease" not in [call["name"] for call in calls]
    assert calls[1]["name"] == "deleteReleaseAsset"
    assert calls[1]["args"]["asset_id"] == 123
    assert calls[-1]["args"]["release_id"] == 99


def test_published_release_is_never_modified(assets):
    result = run_publish(assets, {"releases": [{"tag_name": "v0.3.1", "draft": False}]})
    assert "already published" in result["error"]
    assert [call["name"] for call in result["calls"]] == ["listReleases"]


@pytest.mark.parametrize(
    "failure",
    ["listReleases", "generateReleaseNotes", "createRelease", "uploadReleaseAsset"],
)
def test_api_failure_never_publishes_partial_release(assets, failure):
    result = run_publish(assets, {"fail": failure})
    assert result["error"] == "simulated API failure"
    assert "updateRelease" not in [call["name"] for call in result["calls"]]


def test_missing_asset_fails_before_api_calls(assets):
    (assets / "SHA256SUMS").unlink()
    result = run_publish(assets, {})
    assert "ENOENT" in result["error"]
    assert result["calls"] == []
