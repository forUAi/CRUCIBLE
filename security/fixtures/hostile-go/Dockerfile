FROM golang:1.23
COPY . /app
WORKDIR /app
RUN go run probe.go
RUN go build -o /app/server ./cmd/server
EXPOSE 8000
CMD ["/app/server"]
