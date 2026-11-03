FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install -r requirements.txt

COPY tplink_ddns.py tplink_online_hosts.py ./

USER 10001:10001

ENTRYPOINT ["python", "/app/tplink_ddns.py"]
CMD ["--config", "/config/ddns_config.json"]
