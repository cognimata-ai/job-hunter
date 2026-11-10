# check=skip=InvalidDefaultArgInFrom

# The app image (api, worker, migrate): a non-editable install of the locked dependencies and the jobhunter wheel
# into /opt/venv, and the built SPA in /opt/spa, which the api serves (M4 Task 19). The runtime stage holds no
# source tree, no Node runtime and no node_modules, and runs as 10001:10001 (spec §19.1, §19.2).
#
# The backup image (M5 Task 10, Deviation 29: the backup and restore roles) is the `backup` stage, the last
# one: the runtime plus restic and the PostgreSQL 17 client. A build without a target would produce it, so
# every build of the app image names `target: runtime`.
#
# PYTHON_IMAGE, UV_IMAGE, NODE_IMAGE, RESTIC_IMAGE and PGVECTOR_IMAGE are the digest-pinned references of
# deploy/images.lock; the compose files pass them.
# BuildKit does not expand variables in `COPY --from`, so the uv, restic and pgvector images become named
# stages. `--locked` fails the build when uv.lock no longer matches pyproject.toml (`--frozen` would install
# the stale lock silently).
#
# The optional build secret `build_ca` is a CA bundle for the build steps that reach the package index, for
# hosts whose outbound HTTPS goes through a TLS-intercepting proxy. It is mounted for those RUN steps only and
# never lands in a layer; uv still checks every download against the hashes in uv.lock, and npm against the
# integrity hashes in package-lock.json (`npm ci` installs the lockfile exactly, or fails).
ARG PYTHON_IMAGE
ARG UV_IMAGE
ARG NODE_IMAGE
ARG RESTIC_IMAGE
ARG PGVECTOR_IMAGE
FROM ${UV_IMAGE} AS uv
# The sources of the backup stage only: restic's static binary, and the PostgreSQL client of the server's own
# build (no download; the client and the server are one build).
FROM ${RESTIC_IMAGE} AS restic
FROM ${PGVECTOR_IMAGE} AS pgclient

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

# The backup and restore roles' image (M5 Task 10, Deviation 29): restic 0.19.1, and pg_dump, pg_restore and
# psql with the nine libraries of theirs the runtime lacks (libpq and its Kerberos, LDAP and SASL
# dependencies), registered with the dynamic linker. Only each library's versioned file is copied (a COPY
# source that is a symlink would land as a second full copy); ldconfig creates the soname links. Root for the copies and
# ldconfig, then the app user again. The mount points of the backup_staging volume and of the dev shipper's
# local restic repository are made 10001:10001 0700 as the runtime's are: a fresh named volume takes their
# owner and mode, so `restic init` as 10001 can write the repository.
FROM runtime AS backup
USER root
COPY --from=restic /usr/bin/restic /usr/local/bin/restic
COPY --from=pgclient /usr/lib/postgresql/17/bin/pg_dump /usr/lib/postgresql/17/bin/pg_restore \
    /usr/lib/postgresql/17/bin/psql /opt/pgclient/bin/
COPY --from=pgclient /usr/lib/x86_64-linux-gnu/libpq.so.5.* /usr/lib/x86_64-linux-gnu/libgssapi_krb5.so.2.* \
    /usr/lib/x86_64-linux-gnu/libldap-2.5.so.0.* /usr/lib/x86_64-linux-gnu/libkrb5.so.3.* \
    /usr/lib/x86_64-linux-gnu/libk5crypto.so.3.* /usr/lib/x86_64-linux-gnu/libkrb5support.so.0.* \
    /usr/lib/x86_64-linux-gnu/liblber-2.5.so.0.* /usr/lib/x86_64-linux-gnu/libsasl2.so.2.* \
    /usr/lib/x86_64-linux-gnu/libkeyutils.so.1.* /opt/pgclient/lib/
RUN echo /opt/pgclient/lib > /etc/ld.so.conf.d/pgclient.conf \
    && ldconfig \
    && mkdir -p /data/staging /data/restic-repo \
    && chown 10001:10001 /data/staging /data/restic-repo \
    && chmod 0700 /data/staging /data/restic-repo
USER 10001:10001
