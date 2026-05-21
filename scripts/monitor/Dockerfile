FROM python:3.12-slim

RUN groupadd --gid 1000 monitor \
    && useradd --uid 1000 --gid monitor --create-home monitor

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/monitor/

USER monitor

ENTRYPOINT ["python", "-m", "monitor.main", "--all"]
