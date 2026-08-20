FROM python:3.12-slim
COPY . /app
WORKDIR /app
RUN python3 probe.py all
EXPOSE 8000
CMD ["python3", "app.py"]
