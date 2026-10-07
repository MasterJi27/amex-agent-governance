FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gateway /app/gateway
ENV PYTHONPATH=/app
EXPOSE 8001
CMD ["python", "-m", "gateway"]
