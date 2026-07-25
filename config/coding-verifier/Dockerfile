FROM python@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

LABEL local-agent.component="coding-verifier-python" \
      local-agent.recipe="python-3.12.11-pytest-8.4.1-v2"

COPY requirements.lock /tmp/local-agent-requirements.lock
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
      --require-hashes --only-binary=:all: \
      -r /tmp/local-agent-requirements.lock \
    && rm /tmp/local-agent-requirements.lock

ENTRYPOINT ["/usr/local/bin/python"]
