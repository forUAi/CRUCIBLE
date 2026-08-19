FROM golang:1.22-alpine
WORKDIR /src
COPY . .
RUN go build -o /app ./...
ENV APP_PORT=9090
EXPOSE 9090
CMD ["/app"]
