# Postgres 17 with plpython3u, pgsql-http and drex-postgres, plus the mock Drex API on 127.0.0.1:8787 inside the same
# container, so NACE_BASE_URL can be a plain http://127.0.0.1 URL as the functions require. For the tests only.
FROM postgres:17-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-plpython3-17 postgresql-17-http python3-pip openssl \
    && rm -rf /var/lib/apt/lists/*

# Into the Python that plpython3u runs: Debian's own, so --break-system-packages
COPY pyproject.toml README.md LICENSE /src/
COPY src /src/src
RUN pip3 install --no-cache-dir --break-system-packages /src

COPY test/mock /mock
COPY docker/with-mock.sh /usr/local/bin/with-mock.sh

ENV NACE_API_KEY=nace_sk_mmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmm \
    NACE_BASE_URL=http://127.0.0.1:8787/ns/postgres/ok \
    POSTGRES_PASSWORD=postgres
EXPOSE 5432 8787
ENTRYPOINT ["with-mock.sh"]
CMD ["postgres"]
