FROM golang:1.23
COPY . /app
WORKDIR /app
RUN go run goroutines.go
EXPOSE 8000
CMD ["sleep", "3600"]
