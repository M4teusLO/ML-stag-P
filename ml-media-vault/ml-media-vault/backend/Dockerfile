FROM python:3.12-slim

WORKDIR /app

# Dependências do sistema (Pillow precisa de libjpeg/zlib; curl pra healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libjpeg-dev \
    zlib1g-dev \
    curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Cria diretório de mídias caso ainda não exista
RUN mkdir -p /data/media

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
