# The deployer (pos.selfdeploy): git + docker compose + the PersonalOS backend package.
FROM docker:27-cli
RUN apk add --no-cache python3 py3-pip git bash \
    && git config --global --add safe.directory /repo
WORKDIR /app
COPY backend/pyproject.toml ./
COPY backend/src ./src
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir .
ENV PATH="/venv/bin:$PATH"
ENTRYPOINT []
