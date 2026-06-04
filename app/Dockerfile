FROM python:3.11-slim

WORKDIR /code

# Copy requirements first to leverage Docker layer caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

EXPOSE 8000

# Default command (used by the backend service)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]