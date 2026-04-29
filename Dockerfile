FROM python:3.11-alpine AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build
COPY requirements.txt ./requirements.txt
RUN apk add --no-cache --virtual .build-deps git build-base libffi-dev libstdc++ \
    && pip install --upgrade pip \
    && pip install --no-cache-dir -r ./requirements.txt

FROM python:3.11-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /Crisp-Telegram-Bot
COPY --from=build /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY . /Crisp-Telegram-Bot
RUN chmod +x /Crisp-Telegram-Bot/docker-entrypoint.sh

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["python3", "bot.py"]
