FROM nousresearch/hermes-agent:latest

USER root

# Install the Langfuse Python SDK so the bundled
# ``observability/langfuse`` plugin can ship traces. Without it the
# plugin loads but its hooks short-circuit silently
# (`fail-open when optional dep is missing` per upstream code).
# The base image ships ``uv`` (no ``pip``); install via ``uv pip``
# targeting the embedded venv. Pin >=4 because the bundled
# ``observability/langfuse`` plugin in hermes-agent v0.11.0 calls
# ``set_trace_io`` which only exists in Langfuse 4.x — older 3.x
# resolves cleanly but ``finish trace`` fails at runtime
# (``'LangfuseChain' object has no attribute 'set_trace_io'``) and
# every Hermes turn ends without observations.
RUN /usr/local/bin/uv pip install --python /opt/hermes/.venv/bin/python --no-cache-dir 'langfuse>=4,<5'

COPY plugin /opt/hermes-a2a/plugin
COPY docker /opt/hermes-a2a/docker
COPY examples /opt/hermes-a2a/examples

RUN chmod +x /opt/hermes-a2a/docker/entrypoint.sh \
    /opt/hermes-a2a/docker/install-plugin.sh \
    /opt/hermes-a2a/docker/healthcheck.sh && \
    chown -R hermes:hermes /opt/hermes-a2a

USER hermes

EXPOSE 8642 8081

ENTRYPOINT ["/opt/hermes-a2a/docker/entrypoint.sh"]
CMD ["gateway", "run"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=5 CMD ["/opt/hermes-a2a/docker/healthcheck.sh"]
