FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN groupadd --gid 10001 monitor && useradd --uid 10001 --gid monitor --no-create-home monitor \
    && mkdir /data && chown monitor:monitor /data
COPY monitor.py admin.py .
USER 10001:10001

FROM base AS test
COPY installer.py /app/installer.py
COPY tests /app/tests
RUN python -m unittest discover -s tests -v

FROM base AS runtime
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "monitor.py", "healthcheck"]
ENTRYPOINT ["python", "monitor.py"]
CMD ["run"]
