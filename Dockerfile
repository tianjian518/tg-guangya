# TG → 光鸭 自动转存：容器镜像
# 用法见 docker-compose.yml。所有用户数据落在 /data（挂载宿主机卷），重装不丢。
FROM python:3.11-slim

WORKDIR /app

# 系统依赖（qrcode 用纯 Python 实现，无需额外系统库；telethon/guessit 纯 Python）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码（含 web 面板）
COPY . .

# 数据目录（运行时确保存在；宿主机卷应挂到这里）
RUN mkdir -p /data

ENV DATA_DIR=/data
ENV HOST=0.0.0.0
ENV PORT=8000

EXPOSE 8000

# 直接启动 Web 管理面板（面板内可启动/停止监听 worker）
CMD ["sh", "-c", "python web/server.py --host ${HOST} --port ${PORT}"]
