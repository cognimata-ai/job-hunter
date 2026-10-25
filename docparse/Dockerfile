# check=skip=InvalidDefaultArgInFrom

# The docparse image (spec §3.2, §12.2, §19.1): the `docparse` dependency group (pypdf, pdfplumber,
# python-docx) in /opt/venv and the docparse package in /app/docparse. It holds no jobhunter code and no
# secret, runs as 10002:10002 and writes nothing at run time but the parse_jobs files (the service runs
# read-only, without a network).
#
# PYTHON_IMAGE and UV_IMAGE are the digest-pinned references of deploy/images.lock, as for the app image; the
# build context is the repository root:
#
#   docker build -f docparse/Dockerfile --build-arg PYTHON_IMAGE=<python=...> --build-arg UV_IMAGE=<uv=...> \
#       -t jobhunter-docparse:dev .
#
# `--locked` fails the build when uv.lock no longer matches pyproject.toml. The optional build secret
# `build_ca` is a CA bundle for hosts whose outbound HTTPS goes through a TLS-intercepting proxy (the package
# index); it is mounted for that RUN step only and never lands in a layer, and uv still checks every package
# against the hashes in uv.lock.
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
    uv sync --locked --no-dev --no-install-project --only-group docparse

FROM ${PYTHON_IMAGE} AS runtime
COPY --from=build /opt/venv /opt/venv
# The whole package, so no module of it is ever left out; .dockerignore keeps this Dockerfile and caches out.
COPY docparse/ /app/docparse/
# PYTHONPATH: the loop runs as `python -m docparse.worker`, and each job's spawn child imports docparse too.
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app
WORKDIR /
USER 10002:10002
CMD ["python", "-m", "docparse.worker"]
