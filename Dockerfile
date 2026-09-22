FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agentflow/ ./agentflow/
COPY config/ ./config/
COPY knowledge/ ./knowledge/
COPY web/ ./web/
COPY samples/ ./samples/

# Run unprivileged: the service takes actions on real systems, so give it the
# smallest blast radius the runtime allows.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"

CMD ["python", "-m", "uvicorn", "agentflow.api:app", "--host", "0.0.0.0", "--port", "8000"]
