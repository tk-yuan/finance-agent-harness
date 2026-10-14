# 一个能一键起服务的镜像。
#
# 为什么不把依赖装到宿主机就行：本项目要 Python 3.13 + 一批固定版本的包。
# 别人机器上可能是 3.11，或者 pip 装的时候版本冲突。镜像把这些差异抹平了。
FROM python:3.13-slim

WORKDIR /app

# 先拷依赖清单再装：这样改代码不会让依赖层缓存失效，重建快得多
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 让 print 立刻输出，否则 docker logs 里看不到进度
ENV PYTHONUNBUFFERED=1

LABEL org.opencontainers.image.title="finance-agent-harness" \
      org.opencontainers.image.description="企业财务多智能体系统（自研 Agent 运行时）" \
      org.opencontainers.image.source="https://github.com/tk-yuan/finance-agent-harness"

EXPOSE 8000

# 默认起服务：
#   /         业务界面（记账 / 审核 / 对账 / 报表 / 财务分析）
#   /console  Agent 执行链路与审批面板
CMD ["python", "scripts/serve.py", "0.0.0.0", "8000"]
