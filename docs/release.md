# Release Process

GoSync uses two Docker publishing paths:

- Pull requests build the Docker image for validation only. They do not publish
  a GHCR image.
- Pushes to `main`, including merged pull requests, publish rolling image tags:
  `latest`, `main`, and `sha-<commit>`.
- Version tags like `v1.2.0` publish immutable release tags: `1.2.0` and
  `1.2`.

Do not bump the app version in every pull request. Bump the version only when
preparing an intentional release.

## Normal Pull Requests

For feature, fix, dependency, and documentation pull requests:

1. Do not update `src/gosync/__init__.py`.
2. Do not update release defaults just to merge the PR.
3. Let CI run tests and build the Docker image without publishing it.

After the PR merges, the push to `main` publishes:

```text
ghcr.io/prabhanshuattri/gosync:latest
ghcr.io/prabhanshuattri/gosync:main
ghcr.io/prabhanshuattri/gosync:sha-<commit>
```

## Prepare A Release

When you want a stable release, update every release-facing version reference:

- `src/gosync/__init__.py`
- `README.md`
- any release notes or docs that mention the current version

The Docker workflow reads the package version from `src/gosync/__init__.py` and
passes it into the image build as `GOSYNC_VERSION`.

Verify the version and compose file:

```bash
PYTHONPATH=src python -m gosync --version
docker compose config
```

## Commit And Push

Commit the release changes:

```bash
git status
git add README.md src/gosync/__init__.py docs/release.md
git commit -m "Release GoSync <version>"
git push origin main
```

The push to `main` publishes the rolling tags. It does not publish the immutable
release tag until you create the matching git tag.

## Publish The Version Tag

Create and push a matching signed git tag:

```bash
git tag -s v<version> -m "Release GoSync <version>"
git push origin v<version>
```

The GitHub Actions workflow publishes the versioned image after the tag push.
Wait for the workflow to complete before installing the versioned image in
TrueNAS or Docker Compose.

## Verify The Image

After the workflow succeeds, confirm the versioned image can be pulled:

```bash
docker pull ghcr.io/prabhanshuattri/gosync:<version>
```

If TrueNAS reports `manifest unknown`, the version tag has not been published
yet or the workflow failed before pushing the image.
