# syntax=docker/dockerfile:1
FROM node:22-alpine AS frontend
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
COPY integrated/frontend/ /overlay/
COPY packaging/integrated/prepare_frontend.py /prepare_frontend.py
RUN apk add --no-cache python3 && python3 /prepare_frontend.py /build /overlay && npm run build

# This digest and the source checksums are recorded in packaging/upstream.lock.json.
FROM public.ecr.aws/sliamb/paopaodns@sha256:df0f5689934b3ebb757b7bce0590cd27b97ba23173908eb7205587859012e5c4
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
