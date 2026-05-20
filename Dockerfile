# Use the official Microsoft Playwright image (includes all browser dependencies)
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Set the working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY . .

# Run the pipeline
CMD ["python", "run_pipeline.py", "--final-output", "final_north_india_deep_contacts.xlsx"]