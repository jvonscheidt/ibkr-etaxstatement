"""Exercise WinGet submission using Node and mocked APIs, never live mutations."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
IDENTIFIER = "jvonscheidt.ibkr-etaxstatement"
VERSION = "0.3.1"
PACKAGE_PATH = "manifests/j/jvonscheidt/ibkr-etaxstatement"
FILENAMES = [
    f"{IDENTIFIER}.installer.yaml",
    f"{IDENTIFIER}.locale.en-US.yaml",
    f"{IDENTIFIER}.yaml",
]
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")

HARNESS = r"""
const fs = require("fs")
const path = require("path")
const submit = require(process.env.SUBMIT_SCRIPT)
const scenario = JSON.parse(fs.readFileSync(0, "utf8"))
const calls = []
const notices = []
fs.existsSync = directory => {
  if (directory !== process.env.WINGET_MANIFEST_DIR) throw new Error("wrong directory")
  return scenario.directoryExists !== false
}
fs.statSync = () => ({isDirectory: () => scenario.isDirectory !== false})
fs.readdirSync = () => Object.keys(scenario.manifests).map(name => ({
  name, isFile: () => name !== scenario.nonFile,
}))
fs.readFileSync = (filename, encoding) => {
  if (path.dirname(filename) !== process.env.WINGET_MANIFEST_DIR)
    throw new Error("wrong manifest source")
  if (encoding !== "utf8") throw new Error("wrong encoding")
  return scenario.manifests[path.basename(filename)]
}
const responses = {
  getRef: {object: {sha: "fork-master"}},
  getCommit: {tree: {sha: "fork-master-tree"}},
  createTree: {sha: "manifest-tree"},
  createCommit: {sha: "manifest-commit"},
  createRef: {},
  create: {html_url: "https://example.test/pr/new"},
}
const method = name => async args => {
  calls.push({name, args})
  if (scenario.fail === name) {
    const error = new Error("simulated API failure")
    error.status = scenario.status || 403
    throw error
  }
  if (name === "issuesAndPullRequests") return {data: {items: scenario.prs || []}}
  if (name === "get") return {data: scenario.prs.find(pr => pr.number === args.pull_number)}
  if (name === "getContent") {
    const isVersion = args.path.endsWith("/0.3.1")
    if (isVersion ? scenario.versionExists : scenario.packageExists) return {data: []}
    const error = new Error("Not Found")
    error.status = 404
    throw error
  }
  if (!(name in responses)) throw new Error(`Forbidden mutation: ${name}`)
  return {data: responses[name]}
}
const github = {
  rest: {
    search: {issuesAndPullRequests: method("issuesAndPullRequests")},
    repos: {
      getContent: method("getContent"),
      mergeUpstream: method("mergeUpstream"),
      createOrUpdateFileContents: method("createOrUpdateFileContents"),
    },
    git: Object.fromEntries(
      ["getRef", "getCommit", "createTree", "createCommit", "createRef", "updateRef"]
        .map(name => [name, method(name)]),
    ),
    pulls: {get: method("get"), create: method("create")},
  },
  paginate: async (api, args) => (await api(args)).data.items,
}
const core = {notice: text => notices.push(text)}
const context = {repo: {owner: "jvonscheidt", repo: "ibkr-etaxstatement"}, runId: 123}
submit({github, context, core}).then(
  () => console.log(JSON.stringify({calls, notices})),
  error => console.log(JSON.stringify({calls, notices, error: error.message})),
)
"""


def run_submit(scenario=None, tag="v0.3.1", manifest_directory=True):
    scenario = {
        "manifests": {
            name: (
                f"PackageIdentifier: {IDENTIFIER}\n"
                f"PackageVersion: {VERSION}\n"
                "# generated artifact, not the checked-in template\n"
            )
            for name in FILENAMES
        },
        **(scenario or {}),
    }
    environment = {
        **os.environ,
        "SUBMIT_SCRIPT": str(ROOT / ".github" / "scripts" / "submit-initial-winget.js"),
        "GITHUB_RUN_ATTEMPT": "2",
    }
    for key in ("RELEASE_TAG", "WINGET_MANIFEST_DIR"):
        environment.pop(key, None)
    if tag is not None:
        environment["RELEASE_TAG"] = tag
    if manifest_directory:
        environment["WINGET_MANIFEST_DIR"] = str(ROOT / "generated-manifest-artifact")
    result = subprocess.run(
        [NODE, "-e", HARNESS],
        env=environment,
        input=json.dumps(scenario),
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("package_exists", [False, True])
def test_creates_atomic_manifest_commit_from_unsynced_fork(package_exists):
    result = run_submit({"packageExists": package_exists})
    assert "error" not in result
    calls = result["calls"]
    assert [call["name"] for call in calls] == [
        "issuesAndPullRequests",
        "getContent",
        "getContent",
        "getRef",
        "getCommit",
        "createTree",
        "createCommit",
        "createRef",
        "create",
    ]
    assert "is:open" not in calls[0]["args"]["q"]
    assert calls[1]["args"]["path"] == f"{PACKAGE_PATH}/{VERSION}"
    assert calls[2]["args"]["path"] == PACKAGE_PATH
    assert calls[3]["args"]["owner"] == "jvonscheidt"
    assert calls[3]["args"]["ref"] == "heads/master"
    assert calls[4]["args"]["commit_sha"] == "fork-master"
    tree = calls[5]["args"]
    assert tree["base_tree"] == "fork-master-tree"
    assert [entry["path"] for entry in tree["tree"]] == [
        f"{PACKAGE_PATH}/{VERSION}/{name}" for name in FILENAMES
    ]
    for entry in tree["tree"]:
        assert entry["mode"] == "100644"
        assert entry["type"] == "blob"
        assert "# generated artifact" in entry["content"]
    assert calls[6]["args"]["parents"] == ["fork-master"]
    assert calls[6]["args"]["tree"] == "manifest-tree"
    assert calls[7]["args"]["sha"] == "manifest-commit"
    branch = f"new-{IDENTIFIER}-{VERSION}-123-2"
    assert calls[7]["args"]["ref"] == f"refs/heads/{branch}"
    pr = calls[8]["args"]
    prefix = "New version" if package_exists else "New package"
    assert pr["title"] == f"{prefix}: {IDENTIFIER} version {VERSION}"
    assert pr["head"] == f"jvonscheidt:{branch}"
    assert pr["owner"] == "microsoft"
    assert pr["base"] == "master"
    assert "- [ ] Tested manifest locally" in pr["body"]
    assert "- [ ] Signed the" in pr["body"]
    assert "three manifest files" in pr["body"]
    assert result["notices"] == ["https://example.test/pr/new"]


@pytest.mark.parametrize(
    "tag",
    [
        None,
        "",
        "0.3.1",
        "v0.3",
        "v0.3.1-rc1",
        "v0.3.1+build",
        "v00.3.1",
        "v0.03.1",
        "v0.3.01",
        "v0.3.1\n",
        "v0.3.1/other",
    ],
)
def test_invalid_tag_fails_before_api_calls(tag):
    result = run_submit(tag=tag)
    assert "Unsupported release tag" in result["error"]
    assert result["calls"] == []


def test_missing_manifest_environment_never_falls_back_to_checked_in_files():
    result = run_submit(manifest_directory=False)
    assert "WINGET_MANIFEST_DIR is required" in result["error"]
    assert result["calls"] == []


@pytest.mark.parametrize(
    "scenario",
    [
        {"directoryExists": False},
        {"isDirectory": False},
        {"nonFile": FILENAMES[0]},
        {"manifests": {}},
        {"manifests": {name: "" for name in FILENAMES[:2]}},
        {"manifests": {name: "" for name in [*FILENAMES, "unexpected.yaml"]}},
        {"manifests": {name: "" for name in [*FILENAMES[:2], "wrong.yaml"]}},
    ],
)
def test_invalid_manifest_directory_fails_before_api_calls(scenario):
    result = run_submit(scenario)
    assert "error" in result
    assert result["calls"] == []


@pytest.mark.parametrize("filename", FILENAMES)
@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("PackageVersion", "PackageVersion: 0.3.0"),
        ("PackageVersion", "PackageVersion: 0.3.10"),
        ("PackageVersion", "# missing version"),
        ("PackageVersion", "PackageVersion: 0.3.1\nPackageVersion: 0.3.1"),
        ("PackageIdentifier", "PackageIdentifier: different.package"),
        ("PackageIdentifier", "# missing identifier"),
        (
            "PackageIdentifier",
            f"PackageIdentifier: {IDENTIFIER}\nPackageIdentifier: {IDENTIFIER}",
        ),
    ],
)
def test_invalid_manifest_fields_fail_before_api_calls(filename, key, replacement):
    manifests = {
        name: f"PackageIdentifier: {IDENTIFIER}\nPackageVersion: {VERSION}\n"
        for name in FILENAMES
    }
    expected = VERSION if key == "PackageVersion" else IDENTIFIER
    manifests[filename] = manifests[filename].replace(f"{key}: {expected}", replacement)
    result = run_submit({"manifests": manifests})
    assert f"{filename}: {key} must be" in result["error"]
    assert result["calls"] == []


def pull_request(state="open", merged=False, version=VERSION):
    return {
        "number": 42,
        "title": f"New version: {IDENTIFIER} version {version}",
        "state": state,
        "merged_at": "2026-09-06T00:00:00Z" if merged else None,
        "html_url": "https://example.test/pr/existing",
    }


@pytest.mark.parametrize(("state", "merged"), [("open", False), ("closed", True)])
def test_existing_open_or_merged_pr_is_idempotent(state, merged):
    result = run_submit({"prs": [pull_request(state, merged)]})
    assert "error" not in result
    assert [call["name"] for call in result["calls"]] == [
        "issuesAndPullRequests",
        "get",
    ]
    assert result["notices"] == ["https://example.test/pr/existing"]


def test_closed_unmerged_pr_can_retry():
    result = run_submit({"prs": [pull_request("closed")]})
    assert "error" not in result
    assert result["calls"][-1]["name"] == "create"


def test_near_match_version_does_not_suppress_submission():
    result = run_submit({"prs": [pull_request(version="0.3.10")]})
    assert "error" not in result
    assert "get" not in [call["name"] for call in result["calls"]]
    assert result["calls"][-1]["name"] == "create"


def test_existing_upstream_version_is_idempotent():
    result = run_submit({"versionExists": True})
    assert "error" not in result
    assert [call["name"] for call in result["calls"]] == [
        "issuesAndPullRequests",
        "getContent",
    ]
    assert "already exists upstream" in result["notices"][0]


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_upstream_lookup_errors_propagate_without_mutation(status):
    result = run_submit({"fail": "getContent", "status": status})
    assert result["error"] == "simulated API failure"
    assert [call["name"] for call in result["calls"]] == [
        "issuesAndPullRequests",
        "getContent",
    ]


@pytest.mark.parametrize("failure", ["issuesAndPullRequests", "get"])
def test_pr_lookup_errors_propagate_without_mutation(failure):
    result = run_submit({"fail": failure, "prs": [pull_request()]})
    assert result["error"] == "simulated API failure"
    assert all(
        call["name"] in ("issuesAndPullRequests", "get") for call in result["calls"]
    )


@pytest.mark.parametrize("failure", ["createTree", "createCommit", "createRef"])
def test_git_failure_never_opens_partial_pr(failure):
    result = run_submit({"fail": failure})
    assert result["error"] == "simulated API failure"
    assert result["calls"][-1]["name"] == failure
    assert "create" not in [call["name"] for call in result["calls"]]
