# check=skip=InvalidDefaultArgInFrom

# The embedder image (spec §2.1 U3, §3.2, §19.1): the `embedder` dependency group in /opt/venv, the model files of
# embedder/model.lock in /opt/model, and app.py, health.py and model.lock in /app. It holds no jobhunter code,
# runs as 10004:10004 and writes nothing at run time (the service runs read-only).
#
# PYTHON_IMAGE and UV_IMAGE are the digest-pinned references of deploy/images.lock, as for the app image; the
# build context is the repository root:
#
#   docker build -f embedder/Dockerfile --build-arg PYTHON_IMAGE=<python=...> --build-arg UV_IMAGE=<uv=...> \
#       -t jobhunter-embedder:dev .
#
# download_model.sh fetches the locked files from Hugging Face at the locked revision and checks each one with
# `sha256sum --check` against model.lock, so a different file fails the build; main() checks /opt/model against
# the lock again at every start (§16.8 check 7). curl exists in the download stage only. `--locked` fails the
# build when uv.lock no longer matches pyproject.toml.
#
# The optional build secret `build_ca` is a CA bundle for hosts whose outbound HTTPS goes through a
# TLS-intercepting proxy (the package index, the Debian mirror, Hugging Face). It is mounted for those RUN steps
# only and never lands in a layer; uv still checks every package against uv.lock, curl's downloads against
# model.lock.
ARG PYTHON_IMAGE
ARG UV_IMAGE
FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv sync --locked --no-dev --no-install-project --only-group embedder

FROM ${PYTHON_IMAGE} AS model
# The Debian sources over HTTPS, so a proxy that only tunnels HTTPS can carry them; apt downloads as `_apt`,
# hence the secret's mode.
RUN --mount=type=secret,id=build_ca,required=false,mode=0444 \
    set -eu; \
    if [ -s /run/secrets/build_ca ]; then set -- -o Acquire::https::CAInfo=/run/secrets/build_ca; fi; \
    sed -i 's#http://deb.debian.org/#https://deb.debian.org/#' /etc/apt/sources.list.d/debian.sources; \
    apt-get "$@" update; \
    apt-get "$@" install -y --no-install-recommends curl; \
    rm -rf /var/lib/apt/lists/*
COPY embedder/download_model.sh embedder/model.lock /build/
RUN --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then export CURL_CA_BUNDLE=/run/secrets/build_ca; fi; \
    sh /build/download_model.sh /opt/model

FROM ${PYTHON_IMAGE} AS runtime
COPY --from=build /opt/venv /opt/venv
COPY --from=model /opt/model /opt/model
COPY embedder/app.py embedder/health.py embedder/model.lock /app/
RUN chmod 0555 /app/health.py && ln -s /app/health.py /usr/local/bin/embedder-health
# onnxruntime's Microsoft telemetry off, the Hugging Face hub offline and without telemetry (app.py's
# RUNTIME_ENV, which OnnxModel sets again before its imports)
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 ORT_DISABLE_TELEMETRY=1 HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1
WORKDIR /
USER 10004:10004
EXPOSE 8080
CMD ["python", "/app/app.py"]
