# Runs the platform with no third-party packages: nothing is installed.
FROM python:3.12-slim
RUN useradd --create-home --uid 10001 civicexp
WORKDIR /app
COPY src ./src
ENV PYTHONPATH=/app/src CIVICEXP_WORKSPACE=/data
RUN mkdir /data && chown civicexp /data
USER civicexp
VOLUME /data
EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
# Put a TLS-terminating proxy in front; it handles certificates.
CMD ["python", "-m", "civicexp", "serve", "--host", "0.0.0.0", "--port", "8080", "--init-if-missing", "--secure-cookies", "--trust-proxy"]
