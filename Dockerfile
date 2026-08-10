FROM python:3.12-slim

# Keeps Python from writing .pyc files and makes logs appear immediately.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

# Install dependencies first, in their own layer.
# Docker caches this step, so editing app code does not reinstall packages.
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e ".[dev]"

COPY . .

# The editable install above runs before the source is copied, so it registers no
# packages. Source is mounted at /code at runtime anyway, so put it on the import
# path directly. Without this, uvicorn works (it adds the working directory) but
# pytest does not.
ENV PYTHONPATH=/code

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
