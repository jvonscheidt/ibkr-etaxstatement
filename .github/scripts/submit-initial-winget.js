const fs = require("fs")
const path = require("path")

module.exports = async ({ github, context, core }) => {
  const identifier = "jvonscheidt.ibkr-etaxstatement"
  const forkOwner = context.repo.owner
  const repository = "winget-pkgs"
  const version = process.env.RELEASE_TAG.replace(/^v/, "")

  if (!/^\d+\.\d+\.\d+$/.test(version)) {
    throw new Error(`Unsupported release version: ${version}`)
  }

  const manifestDirectory = path.join(
    process.env.GITHUB_WORKSPACE,
    "packaging",
    "winget",
    version,
  )
  if (
    !fs.existsSync(manifestDirectory) ||
    !fs.statSync(manifestDirectory).isDirectory()
  ) {
    throw new Error(`No checked-in manifests found at ${manifestDirectory}`)
  }

  const existingPullRequests =
    await github.rest.search.issuesAndPullRequests({
      q: `repo:microsoft/${repository} is:pr is:open in:title "${identifier}" "${version}"`,
    })
  if (existingPullRequests.data.total_count > 0) {
    core.notice(existingPullRequests.data.items[0].html_url)
    return
  }

  await github.rest.repos.mergeUpstream({
    owner: forkOwner,
    repo: repository,
    branch: "master",
  })
  const base = await github.rest.git.getRef({
    owner: forkOwner,
    repo: repository,
    ref: "heads/master",
  })
  const branch = `new-${identifier}-${version}-${context.runId}-${process.env.GITHUB_RUN_ATTEMPT}`
  await github.rest.git.createRef({
    owner: forkOwner,
    repo: repository,
    ref: `refs/heads/${branch}`,
    sha: base.data.object.sha,
  })

  const targetDirectory = `manifests/j/jvonscheidt/ibkr-etaxstatement/${version}`
  for (const fileName of fs.readdirSync(manifestDirectory).sort()) {
    const content = fs.readFileSync(path.join(manifestDirectory, fileName))
    await github.rest.repos.createOrUpdateFileContents({
      owner: forkOwner,
      repo: repository,
      path: `${targetDirectory}/${fileName}`,
      message: `New package: ${identifier} version ${version}`,
      content: content.toString("base64"),
      branch,
    })
  }

  const body = `## 📖 Description

Adds ${identifier} version ${version}.

## ✅ Checklist

- [ ] Signed the [Contributor License Agreement](https://cla.opensource.microsoft.com)
- [x] Linked to an issue (not applicable)

## 📦 Manifest Checklist

- [x] Checked that there aren't other open pull requests for the same manifest
- [x] This PR only modifies one manifest
- [x] Validated manifest locally with \`winget validate --manifest <path>\`
- [ ] Tested manifest locally with \`winget install --manifest <path>\`
- [x] Manifest conforms to the 1.12 schema
`

  const pullRequest = await github.rest.pulls.create({
    owner: "microsoft",
    repo: repository,
    title: `New package: ${identifier} version ${version}`,
    head: `${forkOwner}:${branch}`,
    base: "master",
    body,
  })
  core.notice(pullRequest.data.html_url)
}
