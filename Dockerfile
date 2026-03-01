FROM python:3.9-slim-bullseye

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY event_tracker.py .

# Run the bot
CMD ["python", "event_tracker.py"]
