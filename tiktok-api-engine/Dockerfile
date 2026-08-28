FROM golang:1.23-alpine AS builder
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY *.go ./
# Static build so the runtime image needs no libc beyond what ffmpeg brings.
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/tiktok-api-engine .

FROM alpine:3.20
# ffmpeg is required for the mp3 transcode and the slideshow render; curl only
# so the container can health-check itself.
RUN apk add --no-cache ffmpeg ca-certificates curl
COPY --from=builder /out/tiktok-api-engine /usr/local/bin/tiktok-api-engine
# Renders write to a temp dir and nothing else, so an unprivileged user is enough.
USER 65532:65532
EXPOSE 9110
ENTRYPOINT ["/usr/local/bin/tiktok-api-engine"]
