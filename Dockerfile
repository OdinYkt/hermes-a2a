FROM nousresearch/hermes-agent:latest

USER root

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
