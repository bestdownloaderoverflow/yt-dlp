FROM golang:1.26-alpine AS builder

ARG WIREPROXY_VERSION=v1.1.2
RUN GOBIN=/out go install github.com/windtf/wireproxy/cmd/wireproxy@${WIREPROXY_VERSION}

FROM alpine:3.20
RUN apk add --no-cache ca-certificates wget
COPY --from=builder /out/wireproxy /usr/local/bin/wireproxy-bin
COPY wireproxy/reconnect-entrypoint.sh /usr/local/bin/wireproxy
RUN chmod 0755 /usr/local/bin/wireproxy
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/wireproxy"]
CMD ["--config", "/etc/wireproxy/wireproxy.conf", "--info", "0.0.0.0:9080"]
