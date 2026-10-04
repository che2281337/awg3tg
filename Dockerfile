FROM docker:27-cli AS dockercli

FROM python:3.12-slim
# Бот управляет контейнером amnezia-awg2 через docker CLI (нужен docker.sock хоста)
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
CMD ["python", "-m", "bot"]
