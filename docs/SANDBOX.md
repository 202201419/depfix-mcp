# The sandbox: setup, proof, and troubleshooting

## One-time setup on macOS

1. Install a Docker-compatible runtime and start it. Either Docker Desktop, or free and open
   source Colima: `brew install colima docker && colima start`.
2. Run `make smoke-docker`. It builds the image (the one step that uses the network), then runs
   `depfix sandbox check`, which proves the isolation from INSIDE a container. Expected:

```
[ok  ] network is blocked
[ok  ] root filesystem is read-only
[ok  ] host secrets are not visible
[ok  ] your home folder is not visible
[ok  ] runs as a non-root user uid=501
[ok  ] a runaway process is killed on timeout

sandbox isolation verified
```

If any row says FAIL, do not use the Docker runner and send me the output.

## Using it

`depfix verify <id>` uses Docker by default. `--runner local --allow-local-exec` is the escape
hatch for code you fully trust.

## Projects that need more libraries

The default image has Python 3.12, pytest, NumPy 2, ruff and mypy only. If your tests import
pandas, build your own image on top:

```dockerfile
FROM depfix-sandbox:py312
RUN pip install --no-cache-dir pandas
```

`docker build -t my-sandbox .` then `depfix verify <id> --image my-sandbox`. The network is still
off when verification runs; only the build uses it.

## Troubleshooting

| Message | Meaning and fix |
|---------|-----------------|
| `Docker was not found` | Install Docker Desktop or Colima |
| `Docker is installed but not running` | Start Docker Desktop, or `colima start` |
| `sandbox image ... does not exist yet` | `depfix sandbox build` |
| `docker could not start the container` plus a mount error | Docker may not be allowed to share your temp folder. In Docker Desktop: Settings, Resources, File sharing; make sure `/private`, `/var/folders` and `/Users` are shared |
| `do not run depfix as root` | Run as your normal user; a root sandbox would defeat the point |
| `Unable to find image` for the base image | Docker Hub unreachable during `sandbox build`; retry on a normal connection |
