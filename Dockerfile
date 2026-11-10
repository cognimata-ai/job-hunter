# check=skip=InvalidDefaultArgInFrom

# The app image (api, worker, migrate): a non-editable install of the locked dependencies and the jobhunter wheel
# into /opt/venv, and the built SPA in /opt/spa, which the api serves (M4 Task 19). The runtime stage holds no
# source tree, no Node runtime and no node_modules, and runs as 10001:10001 (spec §19.1, §19.2).
#
# PYTHON_IMAGE, UV_IMAGE and NODE_IMAGE are the digest-pinned references of deploy/images.lock; the compose
# files pass them.
# BuildKit does not expand variables in `COPY --from`, so the uv image becomes a named stage. `--locked` fails the
# build when uv.lock no longer matches pyproject.toml (`--frozen` would install the stale lock silently).
#
# The optional build secret `build_ca` is a CA bundle for the build steps that reach the package index, for
# hosts whose outbound HTTPS goes through a TLS-intercepting proxy. It is mounted for those RUN steps only and
# never lands in a layer; uv still checks every download against the hashes in uv.lock, and npm against the
# integrity hashes in package-lock.json (`npm ci` installs the lockfile exactly, or fails).
ARG PYTHON_IMAGE
ARG UV_IMAGE
ARG NODE_IMAGE
FROM ${UV_IMAGE} AS uv

# The SPA (frontend/): only its `dist` leaves this stage.
FROM ${NODE_IMAGE} AS spa
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/build_ca; fi; \
    npm ci
COPY frontend/ ./
RUN npm run build

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
COPY --from=spa /src/frontend/dist /opt/spa
# The mount points of the cv_store volume (encrypted CV files, §12.1) and, since M5 (R18), of the manifests
# volume (the deletion manifests' files, §19.5) and the exports volume (the encrypted export archives, §20.3),
# each owned by the app user and private to it: Docker gives a fresh named volume the ownership and mode of the
# image's directory.
RUN mkdir -p /data/cv /data/manifests /data/exports \
    && chown 10001:10001 /data/cv /data/manifests /data/exports \
    && chmod 0700 /data/cv /data/manifests /data/exports
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1
WORKDIR /
USER 10001:10001
