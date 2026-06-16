FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# GepcoopPriceAgent forrás /app/GepcoopPriceAgent-ként lesz mountolva
# a docker-compose.yml volumes szekciójában

COPY . /app/batch-price-agent

WORKDIR /app/batch-price-agent

EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
