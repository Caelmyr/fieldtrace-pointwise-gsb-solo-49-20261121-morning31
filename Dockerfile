ARG FIELDTRACE_DEPS_IMAGE=python:3.11-slim
FROM ${FIELDTRACE_DEPS_IMAGE} AS dependencies
USER root
WORKDIR /build/fieldtrace
COPY requirements.txt .
RUN python -m pip install --disable-pip-version-check --no-cache-dir --only-binary=:all: -r requirements.txt && python -m pip check

FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOST=0.0.0.0 PORT=8000 FIELDTRACE_DATA=/var/lib/fieldtrace
WORKDIR /opt/fieldtrace
COPY --from=dependencies /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY fieldtrace ./fieldtrace
COPY static ./static
RUN useradd -U -u 11502 fieldtrace && install -d -o fieldtrace -g fieldtrace /var/lib/fieldtrace
USER fieldtrace
EXPOSE 8000
CMD ["python", "-m", "fieldtrace"]
