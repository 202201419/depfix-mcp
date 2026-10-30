FROM python:3.12-slim
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100 \
    PIP_RETRIES=10 \
    PIP_NO_CACHE_DIR=1 \
    PIP_PREFER_BINARY=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN pip install "pytest>=8,<9"
RUN pip install "numpy>=2,<3"
RUN pip install "ruff>=0.6" "mypy>=1.10"
WORKDIR /work
