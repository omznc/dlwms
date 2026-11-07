FROM python:3.12.8-slim-bookworm

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 FASTEMBED_CACHE=/opt/models
RUN pip install fastembed==0.9.1 sqlite-vec==0.1.9 starlette==0.46.2 uvicorn==0.34.3 jinja2==3.1.6

WORKDIR /app
COPY common.py app.py ./
COPY templates templates
COPY static static

# Bake the embedding model into the image so startup needs no download.
RUN python -c "from common import load_model; load_model()"
ENV HF_HUB_OFFLINE=1 INDEX_DB=/data/index.db FILES_DIR=/data/files HOST=0.0.0.0 PORT=8000

RUN useradd --system --no-create-home app
USER app
EXPOSE 8000
CMD ["python", "app.py"]
