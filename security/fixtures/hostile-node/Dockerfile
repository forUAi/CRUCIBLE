FROM node:22-slim
COPY . /app
WORKDIR /app
RUN npm install --no-audit --no-fund
EXPOSE 8000
CMD ["node", "app.js"]
