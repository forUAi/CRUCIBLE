FROM python:3.12-slim
COPY . /app
WORKDIR /app
RUN python3 fill.py
EXPOSE 8000
CMD ["python3", "app.py"]
