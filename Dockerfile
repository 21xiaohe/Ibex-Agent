# Dockerfile —— Ibex 应用镜像（web / worker / 飞书接收端共用，启动命令不同）
# 国内服务器用 pip + 清华源，避开 uv 在国内源上的卡死问题
FROM python:3.13-slim

WORKDIR /app

# pip 走清华源（国内服务器从国外 PyPI 下载会很慢/超时）
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

# 先拷依赖清单装依赖（利用 Docker 层缓存：依赖不变就不重装）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码
COPY . .

# 默认启动 web（worker/飞书容器用 command 覆盖）。
# 注意：Linux 容器里不需要 Windows 那套事件循环处理；
# start_web.sh 里设了 Prometheus 多进程目录（见下）。
CMD ["bash", "scripts/start_web.sh"]