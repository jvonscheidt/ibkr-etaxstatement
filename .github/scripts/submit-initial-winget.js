const fs = require("fs")
const path = require("path")

module.exports = async ({ github, context, core }) => {
  const identifier = "jvonscheidt.ibkr-etaxstatement"
  const forkOwner = context.repo.owner
  const repository = "winget-pkgs"
  const tag = process.env.RELEASE_TAG
  const match = tag?.match(/^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/)

  if (!match || match[0] !== tag) {
    throw new Error(`Unsupported release tag: ${tag}`)
  }
  const version = tag.slice(1)

  const manifestDirectory = process.env.WINGET_MANIFEST_DIR
  if (!manifestDirectory) {
    throw new Error("WINGET_MANIFEST_DIR is required")
  }
  if (
    !fs.existsSync(manifestDirectory) ||
    !fs.statSync(manifestDirectory).isDirectory()
  ) {
    throw new Error(`No generated manifests found at ${manifestDirectory}`)
  }
  const expectedFiles = [
    `${identifier}.installer.yaml`,
    `${identifier}.locale.en-US.yaml`,
    `${identifier}.yaml`,
  ]
  const entries = fs.readdirSync(manifestDirectory, { withFileTypes: true })
  if (
    entries.length !== expectedFiles.length ||
    entries.some(entry => !entry.isFile() || !expectedFiles.includes(entry.name))
  ) {
    throw new Error("Manifest directory must contain exactly the three expected YAML files")
  }
  const manifests = expectedFiles.map(fileName => {
    const content = fs.readFileSync(path.join(manifestDirectory, fileName), "utf8")
    for (const [key, expected] of [
      ["PackageIdentifier", identifier],
      ["PackageVersion", version],
    ]) {
      const values = [...content.matchAll(new RegExp(`^${key}:[ \\t]*(.*)$`, "gm"))]
      if (values.length !== 1 || values[0][1].trim() !== expected) {
        throw new Error(`${fileName}: ${key} must be ${expected}`)
      }
    }
    return { fileName, content }
  })

  const existingPullRequests = await github.paginate(
    github.rest.search.issuesAndPullRequests,
    {
      q: `repo:microsoft/${repository} is:pr in:title "${identifier}" "${version}"`,
      per_page: 100,
    },
  )
  for (const issue of existingPullRequests) {
    // Search tokenizes punctuation, so reject near matches such as 0.3.10.
    const tokens = issue.title.match(/[A-Za-z0-9_.-]+/g) || []
    if (!tokens.includes(identifier) || !tokens.includes(version)) continue
    const pullRequest = await github.rest.pulls.get({
      owner: "microsoft",
      repo: repository,
      pull_number: issue.number,
    })
    if (pullRequest.data.state === "open" || pullRequest.data.merged_at) {
      core.notice(pullRequest.data.html_url)
      return
    }
  }

  const packageDirectory = "manifests/j/jvonscheidt/ibkr-etaxstatement"
  const targetDirectory = `${packageDirectory}/${version}`
  const upstreamExists = async directory => {
    try {
      await github.rest.repos.getContent({
        owner: "microsoft",
        repo: repository,
        path: directory,
        ref: "master",
      })
      return true
    } catch (error) {
      if (error.status === 404) return false
      throw error
    }
  }
  if (await upstreamExists(targetDirectory)) {
    core.notice(`${identifier} version ${version} already exists upstream`)
    return
  }
  const prefix = await upstreamExists(packageDirectory) ? "New version" : "New package"
  const title = `${prefix}: ${identifier} version ${version}`

  // Use the fork as-is: syncing upstream workflows would require workflow scope.
  const base = await github.rest.git.getRef({
    owner: forkOwner,
    repo: repository,
    ref: "heads/master",
  })
  const baseCommit = await github.rest.git.getCommit({
    owner: forkOwner,
    repo: repository,
    commit_sha: base.data.object.sha,
  })
  const tree = await github.rest.git.createTree({
    owner: forkOwner,
    repo: repository,
    base_tree: baseCommit.data.tree.sha,
    tree: manifests.map(({ fileName, content }) => ({
      path: `${targetDirectory}/${fileName}`,
      mode: "100644",
      type: "blob",
      content,
    })),
  })
  const commit = await github.rest.git.createCommit({
    owner: forkOwner,
    repo: repository,
    message: title,
    tree: tree.data.sha,
    parents: [base.data.object.sha],
  })
  const branch = `new-${identifier}-${version}-${context.runId}-${process.env.GITHUB_RUN_ATTEMPT || "1"}`
  await github.rest.git.createRef({
    owner: forkOwner,
    repo: repository,
    ref: `refs/heads/${branch}`,
    sha: commit.data.sha,
  })

  const body = `## 📖 Description

Adds ${identifier} version ${version}.

## ✅ Checklist

- [ ] Signed the [Contributor License Agreement](https://cla.opensource.microsoft.com)
- [x] Linked to an issue (not applicable)

## 📦 Manifest Checklist

- [x] Checked that there aren't other open pull requests for the same manifest
- [x] This PR only adds one package version (three manifest files)
- [x] Validated generated manifests in the release workflow with \`winget validate --manifest <path>\`
- [ ] Tested manifest locally with \`winget install --manifest <path>\`
- [x] Manifest conforms to the 1.12 schema

The package maintainer must complete the CLA if requested.
`

  const pullRequest = await github.rest.pulls.create({
    owner: "microsoft",
    repo: repository,
    title,
    head: `${forkOwner}:${branch}`,
    base: "master",
    body,
  })
  core.notice(pullRequest.data.html_url)
}
