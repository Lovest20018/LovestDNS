ARG BUILDER_IMAGE=public.ecr.aws/docker/library/alpine@sha256:bfe7ceb9f09ec0566c48fb93356e45a47a00be5a1dd14fa5b909e7709c66b034
FROM ${BUILDER_IMAGE} AS builder
COPY packaging/components.lock.json /build/components.lock.json
COPY packaging/components/compile.py /build/compile.py
COPY downloads/ /build/downloads/
ARG BUILD_PACKAGES
RUN apk add --no-cache ${BUILD_PACKAGES}
RUN python3 /build/compile.py
FROM scratch AS bundle
COPY --from=builder /build/output/ /
