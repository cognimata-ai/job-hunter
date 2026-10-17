# check=skip=InvalidDefaultArgInFrom

# The app image (api, worker, migrate): a non-editable install of the locked dependencies and the jobhunter wheel
# into /opt/venv. The runtime stage holds no source tree and runs as 10001:10001 (spec §19.1, §19.2).
#
# PYTHON_IMAGE and UV_IMAGE are the digest-pinned references of deploy/images.lock; the compose files pass them.
# BuildKit does not expand variables in `COPY --from`, so the uv image becomes a named stage. `--locked` fails the
# build when uv.lock no longer matches pyproject.toml (`--frozen` would install the stale lock silently).
#
# The optional build secret `build_ca` is a CA bundle for the build steps that reach the package index, for
# hosts whose outbound HTTPS goes through a TLS-intercepting proxy. It is mounted for those RUN steps only and
# never lands in a layer; uv still checks every download against the hashes in uv.lock.
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
    uv sync --locked --no-dev --no-editable --no-install-project
COPY backend ./backend
RUN --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv sync --locked --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1
WORKDIR /
USER 10001:10001
