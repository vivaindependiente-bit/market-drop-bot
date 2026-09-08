FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY market_drop_bot.py .
CMD ["python", "market_drop_bot.py"]
