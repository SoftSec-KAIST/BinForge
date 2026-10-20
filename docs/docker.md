# Docker example

The [example Dockerfile](Dockerfile) provides an Ubuntu 24.04 container with Python 3.12, `sudo`, and the .NET 10 SDK. Build it from the BinForge repository root, using `docs/` as the build context:

```bash
docker build -f docs/Dockerfile -t binforge:ubuntu24 \
  --build-arg USER_ID="$(id -u)" --build-arg GROUP_ID="$(id -g)" docs
```

Mount the checkout at a stable path so installed toolchains continue to find it. The source archive directory is mounted at the location used by paper mode:

```bash
mkdir -p "$HOME/archive_binforge"
docker run -it --name binforge-dev \
  -v "$PWD:/workspace/BinForge" \
  -v "$HOME/archive_binforge:/home/binforge/archive_binforge" \
  binforge:ubuntu24
```

From inside the container, follow the setup commands in the [README](../README.md#1-prepare-the-environment). The Dockerfile supplies the base environment; those commands install BinForge's configured APT packages, Python requirements, compilers, and B2R2. To re-enter the container later, run `docker start -ai binforge-dev`.
