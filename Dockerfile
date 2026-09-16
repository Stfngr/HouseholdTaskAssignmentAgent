FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN DEBIAN_FRONTEND=noninteractive apt-get update \
    && apt-get install --no-install-recommends --yes tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 household-agent \
    && useradd --uid 10001 --gid household-agent --no-create-home --shell /usr/sbin/nologin household-agent

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && rm -rf /root/.cache

COPY main.py ./
COPY household_agent ./household_agent
COPY config ./config

USER household-agent

ENTRYPOINT ["python", "main.py"]
