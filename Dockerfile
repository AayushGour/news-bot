FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && playwright install chromium

ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
CMD ["python", "-m", "pipeline"]
