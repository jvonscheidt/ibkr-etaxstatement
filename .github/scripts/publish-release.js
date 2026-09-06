const fs = require("fs")
const path = require("path")

module.exports = async ({ github, context }) => {
  const tag = process.env.RELEASE_TAG
  if (!/^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(tag || "")) {
    throw new Error(`Unsupported release tag: ${tag}`)
  }
  const directory = process.env.RELEASE_ASSET_DIR
  if (!directory) throw new Error("RELEASE_ASSET_DIR is required")
  const identifier = "jvonscheidt.ibkr-etaxstatement"
  const files = [
    "ibkr-etaxstatement.exe",
    "SHA256SUMS",
    ...["installer", "locale.en-US", ""].map(kind =>
      path.join("winget", `${identifier}${kind ? `.${kind}` : ""}.yaml`),
    ),
  ].map(file => ({
    name: path.basename(file),
    data: fs.readFileSync(path.join(directory, file)),
  }))

  // Unlike getReleaseByTag, listing releases includes recoverable drafts.
  const matches = (await github.paginate(github.rest.repos.listReleases, {
    ...context.repo,
    per_page: 100,
  })).filter(release => release.tag_name === tag)
  if (matches.some(release => !release.draft)) {
    throw new Error("Release already published; rerun only failed jobs or use a new version.")
  }
  if (matches.length > 1) throw new Error(`Multiple drafts found for ${tag}`)
  let release = matches[0]
  if (!release) {
    const notes = await github.rest.repos.generateReleaseNotes({
      ...context.repo, tag_name: tag, target_commitish: context.sha,
    })
    release = (await github.rest.repos.createRelease({
      ...context.repo, tag_name: tag, target_commitish: context.sha,
      name: tag, body: notes.data.body, draft: true, prerelease: false,
    })).data
  }
  for (const file of files) {
    const old = release.assets.find(asset => asset.name === file.name)
    if (old) await github.rest.repos.deleteReleaseAsset({
      ...context.repo, asset_id: old.id,
    })
    await github.rest.repos.uploadReleaseAsset({
      ...context.repo, release_id: release.id, ...file,
      headers: { "content-type": "application/octet-stream" },
    })
  }
  await github.rest.repos.updateRelease({
    ...context.repo, release_id: release.id, draft: false, make_latest: "true",
  })
}
