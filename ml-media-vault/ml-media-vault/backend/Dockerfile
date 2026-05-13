# Imagem oficial do Playwright já vem com Chromium + dependências
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# Dependências do sistema extras (libjpeg pro Pillow)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg-dev \
    zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Garante que os browsers do Playwright estão presentes (já vêm na imagem, mas reforçamos)
RUN playwright install chromium

COPY app ./app

RUN mkdir -p /data/media

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
