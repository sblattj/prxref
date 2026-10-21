# One image for all three gate functions; each picks its handler with ImageConfig.Command.
# Build context is the repository root (see template.yaml Metadata).
FROM public.ecr.aws/lambda/python:3.12

COPY pyproject.toml README.md LICENSE /build/
COPY src /build/src
RUN pip install --no-cache-dir "/build[aws]" && rm -rf /build

# Lambda's file system is read-only outside /tmp.
ENV HOME=/tmp

CMD ["prxref.gate.aws.receiver_handler"]
