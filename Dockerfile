FROM python:3.13-slim

# Install Node.js 20
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Node deps
COPY mc-bot/package.json mc-bot/
RUN cd mc-bot && npm install --omit=dev

# Copy rest of app
COPY . .

CMD ["python", "bot.py"]


