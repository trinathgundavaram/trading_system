# =============================================================================
#  §42.1 (Phase 3) - the engine image.
#
#  WHY A CONTAINER AT ALL. Not convenience. §41 found that the platform runs
#  three operating systems' worth of untested code paths, and §13 found that
#  engine/ticker_analyzer.py silently falls back to a hand-rolled TA engine
#  when pandas_ta is missing. Three OSes x several Python versions x unpinned
#  numeric libraries is a matrix in which the SAME BARS produce DIFFERENT
#  indicator values, therefore different scores, therefore different trades -
#  with no error and no log line. This image collapses that matrix to one row.
#
#  The property that matters: a v2.0.0 image computes byte-identical
#  indicators on a Mac, on a Linux VPS and on a Windows laptop, in 2026 and in
#  2029. Phase 4 is a change to the decision function whose entire
#  justification is a measured before-and-after; that measurement is only
#  worth anything if both versions compute indicators identically.
# =============================================================================

# Pinned by DIGEST, not by tag. 'python:3.12-slim' is a moving target that
# will silently change the numpy build under you six months from now, which is
# precisely the reproducibility hole this file exists to close.
#
# To pin (do this once, then commit the digest):
#   docker pull python:3.12-slim
#   docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
# and replace the FROM line below with the sha256 it prints. The build works
# without it; the REPRODUCIBILITY GUARANTEE does not.
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/New_York \
    TP_IN_CONTAINER=1

# tzdata is REQUIRED, not optional. scheduler.py's market-hours logic uses
# ZoneInfo('America/New_York'); a slim image has no zone database, so this is
# either ZoneInfoNotFoundError at import or - far worse - a silent UTC
# fallback that shifts every market-open check by four or five hours.
# postgresql-client gives pg_isready and pg_dump for `tp backup`; git is
# needed because storage/version.py reads the commit for the run fingerprint.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata postgresql-client curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies before code, so editing a Python file does not re-resolve every
# wheel. --require-hashes when a hashed lockfile is present; the fallback keeps
# the build working on a tree that has not generated one yet.
COPY requirements.txt requirements.lock.txt* ./
RUN pip install --upgrade pip \
    && (pip install --require-hashes -r requirements.lock.txt 2>/dev/null \
        || pip install -r requirements.txt)

# Verify the reference TA backend is present AT BUILD TIME. Failing here is
# infinitely better than discovering at runtime that this image quietly fell
# back to engine/ta_fallback.py and computed scores that are not comparable
# with any other machine's (§13). This single line is most of the argument for
# the container route.
RUN python -c "import pandas_ta, sys; print('pandas_ta', getattr(pandas_ta, '__version__', 'unknown'))"

# And that the zone database really is readable, for the same reason: a
# market-hours check that is wrong by four hours fails silently, and would
# look exactly like the 22 July 'scheduler stopped firing' incident.
RUN python -c "from zoneinfo import ZoneInfo; ZoneInfo('America/New_York'); print('tzdata ok')"

COPY . .

# Non-root. This container holds brokerage credentials in its environment; a
# root process is one container escape away from the host.
#
# The mkdir before chown is load-bearing. Docker creates a missing volume
# mountpoint as root:root, so declaring VOLUME ["/data"] without the directory
# already existing and owned by uid 1000 gives a container that starts
# cleanly, runs as `trader`, and then cannot write a single log line or fetch
# a Robinhood session - a permission failure at the first write, not at start.
RUN useradd -m -u 1000 trader \
    && mkdir -p /data/output /home/trader/.tokens \
    && chown -R trader:trader /app /data /home/trader
USER trader

# Data lives on a volume, never in the image layer - an image must be
# disposable and a database must not be.
ENV TP_OUTPUT_DIR=/data/output
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
  CMD python scripts/healthcheck.py || exit 1

ENTRYPOINT ["python"]
CMD ["scheduler.py"]
