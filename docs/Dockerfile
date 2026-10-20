FROM ubuntu:24.04

ARG USER_ID=1000
ARG GROUP_ID=1000

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        dotnet-sdk-10.0 \
        git \
        python3 \
        python3-venv \
        sudo \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid "$GROUP_ID" binforge \
    && useradd --uid "$USER_ID" --gid "$GROUP_ID" --create-home --shell /bin/bash binforge \
    && printf 'binforge ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/binforge \
    && chmod 0440 /etc/sudoers.d/binforge

USER binforge
WORKDIR /workspace/BinForge
CMD ["/bin/bash"]
