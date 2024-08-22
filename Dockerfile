FROM lukewiwa/pythons:36-37-38-39-310-311-312

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY --from=ghcr.io/astral-sh/uv:latest /uvx /bin/uvx

RUN ln -sf /usr/local/bin/python3.9 /usr/local/bin/python
RUN ln -sf /usr/local/bin/pip3.9 /usr/local/bin/pip
