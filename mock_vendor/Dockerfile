FROM python:3.11-slim

WORKDIR /code

# Install standard dependencies directly to avoid missing requirements.txt issues
RUN pip install --no-cache-dir fastapi uvicorn pydantic httpx

# Copy the mock vendor application code into the container
COPY . .

EXPOSE 8001

# Run the Uvicorn server binding to port 8001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]