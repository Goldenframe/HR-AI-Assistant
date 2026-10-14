FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN groupadd --gid 10001 bot && useradd --uid 10001 --gid bot --no-create-home bot \
    && mkdir /app/data && chown bot:bot /app/data

COPY xlsx_export.py workload.py review.py trial.py trck.py ai.py atlassian.py bot.py helpers.py store.py survey.py server.py telegram_client.py /app/
COPY fixtures/team.json fixtures/feedback-export-template.xlsx /app/fixtures/
USER 10001:10001
CMD ["python", "server.py"]
