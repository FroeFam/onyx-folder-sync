ARG PYTHON_TAG=3.12
FROM python:${PYTHON_TAG}-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN pip install --no-cache-dir --prefix=/install .

FROM python:${PYTHON_TAG}-slim

COPY --from=builder /install /usr/local

RUN useradd --create-home --uid 1000 app \
    && mkdir /state /data \
    && chown app:app /state
USER app
WORKDIR /home/app

ENTRYPOINT ["onyx-folder-sync"]