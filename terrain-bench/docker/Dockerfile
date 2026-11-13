# Agent and grading sandbox: compilers only, no emulator code. Never COPY task hidden tests or references in here.
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake rustc cargo git ca-certificates xxd file \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir numpy pillow
ENV HOME=/tmp
WORKDIR /work
