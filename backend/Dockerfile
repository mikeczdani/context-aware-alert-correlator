# Python 3.11 slim – kis méretű alap image, production-ready
FROM python:3.11-slim

# Munkakönyvtár a konténeren belül
WORKDIR /app

# Függőségek telepítése először (Docker layer cache kihasználása:
# ha csak a kód változik, a pip install réteget nem kell újraépíteni)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Forráskód másolása
COPY . .

# A FastAPI app a 8000-es porton fut
EXPOSE 8000

# Uvicorn indítása – a main.py az app/ almappában van
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
