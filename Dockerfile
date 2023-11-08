FROM lukewiwa/pythons:36-37-38-39-310-311-312

RUN ln -sf /usr/local/bin/python3.9 /usr/local/bin/python
RUN ln -sf /usr/local/bin/pip3.9 /usr/local/bin/pip

# Install poetry
ENV POETRY_VIRTUALENVS_IN_PROJECT="true"
RUN python -m pip install poetry
