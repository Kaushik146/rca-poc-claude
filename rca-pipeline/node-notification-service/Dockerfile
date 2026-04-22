FROM node:20-alpine

WORKDIR /app

RUN addgroup -S appuser && adduser -S appuser -G appuser

COPY package.json .
RUN npm install --production

COPY . .

USER appuser

EXPOSE 5004

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD node -e "const http = require('http'); const req = http.get('http://localhost:5004/health', (res) => { process.exit(res.statusCode === 200 ? 0 : 1); }); req.on('error', () => process.exit(1));"

CMD ["node", "server.js"]
