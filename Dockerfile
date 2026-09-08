FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY prometheus/ ./prometheus/
COPY buyer_agent.py verify_security.py ./
COPY tests/ ./tests/

ENV PROM_HOST=0.0.0.0 PROM_PORT=8402 PYTHONUNBUFFERED=1
EXPOSE 8402

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s \
  CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8402/health',timeout=8).status_code==200 else 1)"

CMD ["python","-m","uvicorn","prometheus.api.app:app","--host","0.0.0.0","--port","8402"]
