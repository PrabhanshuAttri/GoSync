# Release Process

GoSync uses the package version in `src/gosync/__init__.py` as the source of
truth for releases.

The Docker publishing workflow reads that version, passes it into the Docker
build as `GOSYNC_VERSION`, and publishes these GHCR image tags:

- `ghcr.io/prabhanshuattri/gosync:<version>`
- `ghcr.io/prabhanshuattri/gosync:latest`
- branch, git tag, and sha tags from `docker/metadata-action`

## Prepare A Release

Update every release default to the new version:

- `src/gosync/__init__.py`
- `Dockerfile`
- `docker-compose.yml`
- `README.md`

Then verify the version and compose file:

```bash
PYTHONPATH=src python -m gosync --version
docker compose config
```

## Commit And Push

Commit the release changes:

```bash
git status
git add Dockerfile README.md docker-compose.yml src/gosync/__init__.py src/gosync/app.py src/gosync/config.py src/gosync/templates/index.html src/gosync/web.py .github/workflows/docker-publish.yml docs/release.md
git commit -m "Release GoSync 1.0.3"
git push origin main
```

## Publish The Version Tag

Create and push a matching git tag:

```bash
git tag v1.0.3
git push origin v1.0.3
```

The GitHub Actions workflow publishes the versioned image after the tag push.
Wait for the workflow to complete before installing the versioned image in
TrueNAS or Docker Compose.

## Verify The Image

After the workflow succeeds, confirm the versioned image can be pulled:

```bash
docker pull ghcr.io/prabhanshuattri/gosync:1.0.3
```

If TrueNAS reports `manifest unknown`, the version tag has not been published
yet or the workflow failed before pushing the image.
