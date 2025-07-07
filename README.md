# BinForge

# Supported compile options

## Architecture

- x86_32
- x86_64
- arm_32 (little endian)
- arm_64 (little endian)
- mips_32 (little endian)
- mips_64 (little endian)
- mipseb_32 (big endian)
- mipseb_64 (big endian)
- ppceb_32 (big endian)
- ppceb_64 (big endian)
- ppc_64 (little endian)

## Optimization

- O0
- O1
- O2
- O3
- Os
- Ofast

## Compilers

- GCC
  - gcc-8.5.0
  - gcc-9.5.0
  - gcc-10.5.0
  - gcc-11.5.0
  - gcc-12.4.0
  - gcc-13.3.0
- Clang
  - clang-7.1.0
  - clang-8.0.1
  - clang-9.0.1
  - clang-10.0.1
  - clang-11.1.0
  - clang-12.0.1
  - clang-13.0.1
  - clang-14.0.6
  - clang-15.0.7
  - clang-16.0.6
  - clang-17.0.6
  - clang-18.1.8
  - clang-19.1.7

## Compiler options

- PIE (Position Independent Execution)
- No-inline
- Stack-protector

# How to use

## Build docker

We strongly recommend that you set up a pure Linux environment using the Dockerfile below and clone and use this repository inside that environment.

- Dockerfile sample
```
FROM ubuntu:24.10

RUN apt-get update
RUN apt-get install -y git sudo dialog locales net-tools apt-utils python3-pip
RUN apt-get install -y sudo
RUN groupadd -g 1100 user
RUN useradd -r -m -d /home/user -s /bin/bash -g root -G sudo -u 1100 user

USER user
WORKDIR /home/user
```

- Build docker
```
# Build an image named ubuntu24
docker build -t ubuntu24 .
# Create container named binforge from ubuntu24
docker run --name binforge -it ubuntu24 /bin/bash
# Initial run
docker start binforge
docker exec -it binforge /bin/bash

# Set initial password for `user`
docker exec -it -u root binforge /bin/bash
passwd user
```

- Run docker
```
docker start binforge
docker exec -it binforge /bin/bash
```

## Setup toolchains

Follow these steps to set up BinForge’s environment and toolchains:

- **Create a Python virtual environment**

Install the `python3-venv` package if it is not already installed, then create a dedicated virtual environment for BinForge:

```bash
sudo apt-get install -y python3-venv
python -m venv bfenv
```

- **Install Python dependencies**

Activate the virtual environment and install all required Python packages:

```bash
source ${venv}/bin/activate
pip install -r requirements.txt
```

- **Configure project settings**

You can customize global configuration options such as directory paths, the number of parallel build jobs, or tool locations by editing the YAML configuration file:

```
BinForge/config/defaults.yaml
```

Only override the settings you need to customize; any value that is missing or empty will fall back to the default.

- **Install default system packages**

This script installs a predefined set of packages via APT to ensure the system has the necessary build tools and libraries:

```
python3 scripts/install_default_projects.py
```

- **Download and set up crosstool-ng**

BinForge can integrate with crosstool-ng to build custom cross-compilers:

```bash
python3 scripts/setup_ctng.py
```

- **Set up GCC toolchains**

You can define target architectures and GCC versions in `config/gcc_toolchain_targets.yaml`. The following script will build and install all specified GCC toolchains:

```
# config/gcc_toolchain_targets.yaml
python3 scripts/setup_gcc.py
```

- **Set up LLVM toolchains**

Similarly, to configure and install Clang/LLVM-based toolchains, specify targets in `config/clang_toolchain_targets.yaml` and run:

```
# config/clang_toolchain_targets.yaml
python3 scripts/setup_clang.py
```

- **Cleanup build artifacts**

Optionally, remove temporary files or intermediate build products to free up disk space:

```
python3 scripts/cleanup.py
```

- **Setup Wrappers**
```
python3 scripts/setup_wrapper.py
```

- **Link toolchains into the workspace**

Finally, create symbolic links to all toolchain binaries so they can be easily discovered by BinForge’s build scripts:

```
python3 scripts/link_toolchains.py
```

## Compile Projects and Dependencies

Follow these steps to build all dependencies and compile your target projects:

- **Build dependencies**


```
python3 scripts/compile.py --build-deps
```

- **Compile projects**

Use this command to compile your selected projects. You can customize various options through the provided configuration files:

  - `--base-path`
  The directory where all compiled datasets and build artifacts will be stored.

  - `--config`
  A YAML configuration file specifying compiler settings, optimization levels, target architectures, and other compiler options.

  - `--project-list`
  A YAML file listing the projects to compile. For example, config/paper_projects.yaml contains the set of projects used to reproduce experiments in the paper.

Example:

```
python3 scripts/compile.py \
    --base-path="/home/user/base_dir" \
    --config="config/all_options.yaml" \
    --project-list="config/paper_projects.yaml"
```

## Dependency Check

BinForge can automatically analyze a list of selected projects to detect which required libraries and headers are missing on your system. This helps ensure that all dependencies are installed before you start the build process.

When you run the dependency check, BinForge reports the status of each library and header file, and shows which projects depend on any missing components.

Example output:
```
[*] Libraries:

pthread        : FOUND
rt             : FOUND
attr           : MISSING


[*] Headers:

sys/stat.h                    : FOUND
sys/types.h                   : FOUND
unistd.h                      : FOUND
wchar.h                       : FOUND
atomic_ops.h                  : MISSING
attr_error_context.h          : MISSING
minix_config.h                : MISSING

==> MISSING Libraries required by which projects?

Library 'attr' required by:
  - acl

==> MISSING Headers required by which projects?

Header 'atomic_ops.h' required by:
  - gc

Header 'attr_error_context.h' required by:
  - acl

Header 'minix_config.h' required by:
  - bool
  - acl
```

This output shows:
- Which dependencies are present or missing.
- Which projects require each missing library or header.

## Handle dependencies

To specify dependencies between projects, BinForge uses a YAML configuration file called `config/dep_map.yaml`. This file defines which projects depend on which other libraries or components, enabling BinForge to automatically determine the correct build order.

Each project is listed under the `projects` key with its name and an optional list of dependencies:

```
projects:
  - name: a2ps
    deps:
      - libpaper
      - gc

  - name: gc
    deps:
      - libatomic

  - name: acl
    deps:
      - attr
```

In this example, BinForge will first build `libatomic` because it is required by `gc`. Once `gc` has been built, it will proceed to build `a2ps`, which depends on both `libpaper` and `gc`. This process ensures that all dependencies are built in the proper sequence without manual intervention.
