FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

WORKDIR /app

RUN addgroup --system appuser && adduser --system --ingroup appuser --no-create-home appuser

COPY pyproject.toml README.md ./
COPY app ./app

RUN pip install --no-cache-dir .

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
