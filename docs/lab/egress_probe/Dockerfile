FROM python@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
RUN apt-get update && apt-get install -y --no-install-recommends nftables iptables iproute2 passwd \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 61001 --no-create-home --shell /usr/sbin/nologin pauth-agent
COPY probe.py /probe/probe.py
ENTRYPOINT ["python", "/probe/probe.py"]
