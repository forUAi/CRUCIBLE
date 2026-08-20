FROM eclipse-temurin:21-jdk
COPY . /app
WORKDIR /app
RUN javac Probe.java && java Probe
RUN javac Server.java
EXPOSE 8000
CMD ["java", "Server"]
