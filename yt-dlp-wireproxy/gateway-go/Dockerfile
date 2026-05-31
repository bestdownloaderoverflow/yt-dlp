FROM golang:1.22-alpine AS builder
WORKDIR /app
COPY gateway-go/go.mod ./gateway-go/go.sum* ./
RUN go mod download
COPY gateway-go/ ./
RUN go build -o gateway-go .

FROM alpine:3.20
RUN apk add --no-cache ca-certificates docker-cli ffmpeg
WORKDIR /app
COPY --from=builder /app/gateway-go /app/gateway-go
EXPOSE 9111
CMD ["/app/gateway-go"]
