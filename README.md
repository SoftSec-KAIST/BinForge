# BinForge

BinForge builds binary datasets from open source projects across compilers, target architectures, optimization levels, and compiler flag sets. It supports configuration driven builds and LLM assisted resolution of build issues.

## Supported configurations

### Architectures

- `x86_32`
- `x86_64`
- `arm_32` (little endian)
- `arm_64` (little endian)
- `mips_32` (little endian)
- `mips_64` (little endian)
- `mipseb_32` (big endian)
- `mipseb_64` (big endian)
- `ppceb_32` (big endian)
- `ppceb_64` (big endian)
- `ppc_64` (little endian)

### Optimization levels

- `O0`
- `O1`
- `O2`
- `O3`
- `Os`
- `Ofast`

### Compilers

- GCC
  - `gcc-8.5.0`
  - `gcc-9.5.0`
  - `gcc-10.5.0`
  - `gcc-11.5.0`
  - `gcc-12.4.0`
  - `gcc-13.3.0`
- Clang
  - `clang-7.1.0`
  - `clang-8.0.1`
  - `clang-9.0.1`
  - `clang-10.0.1`
  - `clang-11.1.0`
  - `clang-12.0.1`
  - `clang-13.0.1`
  - `clang-14.0.6`
  - `clang-15.0.7`
  - `clang-16.0.6`
  - `clang-17.0.6`
  - `clang-18.1.8`
  - `clang-19.1.7`

### Compiler flag options

- PIE (`pie`)
- No inlining (`no_inline`)
- Stack protection (`stack_protector`)

`combine_extra_compiler_options: false` (the default) builds the flag values specified in `extra_compiler_options` once. Set it to `true` to build every on/off combination of the options marked `true`.

## How to use

### 1. Prepare the environment

Use Ubuntu 24.04 and run these commands from the repository root. A container can be created using the [Docker example](docs/docker.md). Keep the checkout in the same location after building the toolchains because installed compilers can contain absolute paths.

#### Python environment

Create a Python 3.12 virtual environment and install the Python requirements:

```bash
sudo apt-get update
sudo apt-get install -y python3-venv
python3 -m venv bfenv
source bfenv/bin/activate
python -m pip install -r requirements.txt
python -m pip check
```

Activate the environment with `source bfenv/bin/activate` in each new terminal before running the Python commands below.

#### System packages

Install the configured system packages:

```bash
python scripts/install_default_packages.py --status
python scripts/install_default_packages.py
```

#### Toolchains

Build the pinned crosstool-ng, GCC, and Clang toolchains. Building all versions takes substantial time and disk space:

```bash
python scripts/setup_ctng.py
python scripts/setup_gcc.py --build-bootstrap
python scripts/setup_gcc.py --build-toolchains
python scripts/setup_clang.py
```

#### Compiler links and wrappers

Check the installations, create the compiler links, and install the wrappers:

```bash
python scripts/setup_gcc.py --status
python scripts/setup_clang.py --status
python scripts/link_toolchains.py
python scripts/link_toolchains.py --status
python scripts/setup_wrapper.py --wrap
python scripts/setup_wrapper.py --status
```

`link_toolchains.py` registers system alternatives and may require administrator privileges. To monitor a toolchain build from another terminal, run `python scripts/monitor_compilers.py` after activating the environment there.

#### B2R2 validator

Install the [.NET 10 SDK](https://learn.microsoft.com/en-us/dotnet/core/install/linux-ubuntu-install?pivots=os-linux-ubuntu-2404&tabs=dotnet10), then install and check the pinned B2R2 tools:

```bash
sudo apt-get install -y dotnet-sdk-10.0
python scripts/setup_b2r2.py
python scripts/setup_b2r2.py --status
```

To validate a collected dataset later, run `python scripts/validate_binaries.py /path/to/dataset/bins`.

### 2. Try a small build

From the repository root, build the two projects in [the smoke configuration](config/examples/binkit-smoke.yaml):

```bash
python scripts/compile.py --config config/examples/binkit-smoke.yaml \
  --base-path "$PWD/out/binkit-smoke" --build
```

This config requests 216 variants: two projects × four compilers × nine architectures × three optimization levels × one compiler flag set. Outputs go under the Git-ignored `out/binkit-smoke/` directory.

### 3. Build the datasets

For paper reproduction, download `paper-sources.tar` from the Zenodo record listed in [paper sources](docs/source-reproducibility.md) and place it at `~/archive_binforge/snapshots/paper-sources.tar`. BinForge verifies it against the tracked [manifest](config/source-snapshots/paper-sources.json) and [checksum](config/source-snapshots/paper-sources.tar.sha256), then builds without downloading project sources.

Install the required project dependencies first:

```bash
python scripts/compile.py --config config/binkit.yaml \
  --build-deps --source-mode paper --source-dataset binkit
python scripts/compile.py --config config/popcon.yaml \
  --build-deps --source-mode paper --source-dataset popcon
```

Then build each dataset in a separate output directory:

```bash
python scripts/compile.py --config config/binkit.yaml \
  --base-path "$PWD/out/binkit" --build --source-mode paper --source-dataset binkit
python scripts/compile.py --config config/popcon.yaml \
  --base-path "$PWD/out/popcon" --build --source-mode paper --source-dataset popcon
```

For ordinary use with the sources specified by a YAML config, omit `--source-mode paper` and `--source-dataset`. The default source mode may reuse a local archive. Edit a copy of either config to select fewer projects or build variants.

### 4. Monitor a build

Run the read-only monitor in another terminal. It finds a single active build automatically and retains the latest completed result:

```bash
python scripts/monitor_build.py
```

If several builds are active, select one with `--base-path`. Use `python scripts/monitor_compilers.py` to monitor toolchain builds.

### 5. Resolve build issues

To generate a build configuration with LLM assistance, supply a config and a local API key file:

```bash
python scripts/compile.py --config path/to/config.yaml \
  --auto-resolve --llm-api-key-path path/to/api-key
```

Auto-resolve uses its own dependency state by default. `--auto-resolve-use-extra-deps` explicitly allows it to use dependencies already installed under `tools/extra_deps/`.
