FROM python:3.12-slim

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 可领取审阅工作副本的 SQLite 默认路径（docker-compose 挂命名卷到 /data；
# 不挂卷时该目录仍可写，副本至少在容器生命周期内保留）
RUN mkdir -p /data

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
