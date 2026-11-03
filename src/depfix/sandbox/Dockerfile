# The default depfix sandbox image. Built ONCE with network access (`depfix sandbox build`);
# every verification run afterwards uses it with --network none.
# To test a project that needs more libraries, build your own image FROM this one,
# add `RUN pip install ...`, and pass it with `depfix verify --image your-image`.
# Separate RUN lines on purpose: Docker caches each finished layer, so a retry on a slow
# connection resumes instead of starting over.
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
