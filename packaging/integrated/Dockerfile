ARG FRONTEND_IMAGE=node:22-alpine
FROM ${FRONTEND_IMAGE} AS frontend
WORKDIR /build
COPY integrated/frontend/package.json integrated/frontend/package-lock.json ./
RUN npm ci && npm audit --audit-level=low
COPY web/ ./
COPY integrated/frontend/package.json integrated/frontend/package-lock.json ./
COPY integrated/frontend/ /overlay/
COPY packaging/integrated/prepare_frontend.py /prepare_frontend.py
RUN apk add --no-cache python3 && python3 /prepare_frontend.py /build /overlay && npm run build

# This digest and the source checksums are recorded in packaging/upstream.lock.json.
FROM public.ecr.aws/sliamb/paopaodns@sha256:df0f5689934b3ebb757b7bce0590cd27b97ba23173908eb7205587859012e5c4 AS upstream
WORKDIR /app
RUN apk add --no-cache python3 py3-pip py3-yaml && python3 -m venv --system-site-packages /opt/venv
COPY packaging/integrated/requirements.txt /opt/suite/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /opt/suite/requirements.txt
COPY packaging/upstream.lock.json /opt/suite/upstream.lock.json
COPY packaging/integrated/patch_upstream.py packaging/integrated/generate_supervisor.py /opt/suite/build/
COPY integrated/runtime/ /opt/suite/runtime/
COPY integrated/backend/ /opt/suite/backend/
COPY web/backend/app.py /app/app.py
ENV PYTHONPATH=/opt/suite:/opt/suite/backend:/app \
    DATA_DIR=/data DNS_TEST_SERVER=127.0.0.1 \
    SUITE_STATE_DIR=/var/lib/paopaodns-suite SUITE_RUN_DIR=/run/paopaodns-suite
RUN /opt/venv/bin/python /opt/suite/build/patch_upstream.py \
    && /opt/venv/bin/python /opt/suite/build/generate_supervisor.py /opt/suite/supervisord.conf \
    && cp /opt/suite/upstream.lock.json /opt/suite/build-info.json \
    && /bin/sh -n /usr/sbin/init.sh && /bin/sh -n /usr/sbin/watch_list.sh \
    && /bin/sh -n /usr/sbin/reload.sh
COPY --from=frontend /build/dist /app/dist
VOLUME ["/data", "/var/lib/paopaodns-suite"]
EXPOSE 53/udp 53/tcp 8080/tcp
HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=3 \
    CMD ["/opt/venv/bin/python", "-m", "runtime.health"]
ENTRYPOINT ["/opt/venv/bin/python", "-m", "runtime.entrypoint"]
CMD []

# The upstream target remains available for controlled baseline comparisons.
FROM upstream AS candidate
ARG TARGETARCH
RUN test "${TARGETARCH:-amd64}" = amd64
COPY packaging/components.lock.json /opt/suite/components.lock.json
COPY packaging/components/bundle.py /opt/suite/build/component_bundle.py
COPY .build/components-bundle/ /opt/suite/component-input/
ARG DNS_COMPONENTS=redis,unbound,dnscrypt
RUN /opt/venv/bin/python /opt/suite/build/component_bundle.py \
      --bundle /opt/suite/component-input --lock /opt/suite/components.lock.json --select "$DNS_COMPONENTS" \
    && /opt/venv/bin/python -c 'import hashlib,json,pathlib; p=pathlib.Path("/opt/suite"); lock=json.loads((p/"components.lock.json").read_text()); assert hashlib.sha256(pathlib.Path("/usr/sbin/mosdns").read_bytes()).hexdigest() == lock["frozen"]["mosdns"]["sha256"]; info=json.loads((p/"build-info.json").read_text()); manifest=json.loads((p/"components/manifest.json").read_text()); info["components"]={"selected":manifest["selected"],"lock_sha256":manifest["lock_sha256"],"versions":{k:manifest["components"][k]["version"] for k in manifest["selected"]}}; (p/"build-info.json").write_text(json.dumps(info,sort_keys=True,indent=2)+"\n")' \
    && rm -r /opt/suite/component-input
