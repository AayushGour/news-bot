# T206: mcr.microsoft.com/playwright/python's tags bundle whichever Python
# ships with that Ubuntu release (jammy -> 3.10, noble -> 3.12), and neither
# satisfies pyproject.toml's `requires-python = ">=3.13"` -- the previous
# "jammy" pin never actually built (confirmed: `pip install .` fails with
# "Package 'pipeline' requires a different Python: 3.10.12 not in '>=3.13'",
# and this was true for the `app` service before T206 touched anything, not
# a regression it introduced). Microsoft has no 3.13 Python image, so the
# base image itself has to be 3.13; `playwright install --with-deps` (not
# the old bare `playwright install`) does the apt-get work that image used
# to come with pre-baked.
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY scripts ./scripts
RUN pip install --no-cache-dir . && playwright install --with-deps chromium

ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
CMD ["python", "-m", "pipeline"]
