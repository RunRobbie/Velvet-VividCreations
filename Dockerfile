FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PORT=8000 DATA_DIR=/data
WORKDIR /app
COPY app.py index.html site.js styles.css ./
COPY public/ ./public/
RUN useradd -u 10001 -m appuser && mkdir -p /data && chown -R appuser:appuser /data /app
USER appuser
EXPOSE 8000
CMD ["python","app.py"]
